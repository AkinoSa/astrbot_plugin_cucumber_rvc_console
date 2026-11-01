# -*- coding: utf-8 -*-
"""CUSTOM-6：AstrBot 原生插件页面后端 API。

页面通过 AstrBot bridge 调 ``/api/plug/astrbot_plugin_cucumber_rvc_console/page/*``，
本模块负责：配置读写、模型列表、网关请求、任务记录、RVC 控制台转换。

设计原则：
- 只读复用现有插件实例与 Gateway，不复制推理逻辑；
- 不暴露任意文件读取/命令执行；上传只落在 Gateway 的 uploads 目录；
- 所有返回统一 ``{"status": "ok"|"error", "data"/"message": ...}``。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from quart import request

PLUGIN_NAME = "astrbot_plugin_cucumber_rvc_console"
API_BASE = f"/{PLUGIN_NAME}/page"

# CUSTOM-9 通用化：所有宿主机路径不再写死。
#   插件数据目录 → AstrBot 插件数据路径（自动），可用环境变量 CHORUS_PLUGIN_DATA 覆盖
#   网关工作目录 → 插件配置 `gateway_work_dir`，可用 CHORUS_GATEWAY_WORK 覆盖
#   AstrBot 日志 → AstrBot 数据目录上一级的 info.log，可用 CHORUS_PLUGIN_LOG 覆盖


def _default_plugin_data_dir() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    except Exception:  # noqa: BLE001 — 脱离 AstrBot 时兜底
        return os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)


def _default_log_file() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return os.path.join(os.path.dirname(get_astrbot_data_path().rstrip("/")), "info.log")
    except Exception:  # noqa: BLE001
        return ""


def _data_dir() -> Path:
    return Path(os.environ.get("CHORUS_PLUGIN_DATA") or _default_plugin_data_dir())


def _gateway_work(plugin) -> Path:
    env = os.environ.get("CHORUS_GATEWAY_WORK")
    if env:
        return Path(env)
    try:
        gw = str(plugin.config.get("gateway_work_dir", "") or "").strip()
    except Exception:  # noqa: BLE001
        gw = ""
    return Path(gw) if gw else Path()


def _upload_dir(plugin) -> Path:
    return _gateway_work(plugin) / "uploads"


def _out_dir(plugin) -> Path:
    return _gateway_work(plugin) / "out"


def _task_log_dir(plugin) -> Path:
    return _gateway_work(plugin) / "logs"


def _log_file() -> Path:
    env = os.environ.get("CHORUS_PLUGIN_LOG")
    if env:
        return Path(env)
    p = _default_log_file()
    return Path(p) if p else Path("/dev/null")

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 24 * 1024 * 1024
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus"}
JOB_KEEP = 60
UPLOAD_KEEP = 40

# 只在插件初始化时读取的配置：改完需要重载插件才生效
RELOAD_REQUIRED = {
    # 启动时读取的键全部在此标注（WebUI 保存后显示「需重载插件生效」）
    "enable_rvc", "chorus_seconds", "render_mode", "inference_timeout",
    "default_key_shift", "timeout", "music_api_timeout", "rvc_base_url",
    "rvc_models_keywords", "rvc_models_dir", "source_order",
    "auto_pick_song", "auto_pick_fallback_menu", "auto_pick_model",
    "auto_pick_min_score", "auto_pick_min_margin",
    "enable_smart_cover", "enable_enhanced_context", "enable_confirm_mechanism",
    "enable_batch_cover", "enable_preference_learning", "enable_inject_context",
    "enable_llm_success_notify", "enable_config_report",
    "notify_progress", "notify_retry",
    "enable_auto_key_shift", "male_to_female_shift", "female_to_male_shift",
    "gender_detection_timeout", "enable_auto_key_shift_debug",
    "artist_gender_map", "model_gender_map", "notify_persona_id",
    "enable_send_file", "enable_fade",
    "enable_local_audio_cover", "max_local_audio_size_mb", "local_audio_cache_ttl",
    "local_audio_trigger_keywords", "local_audio_auto_trigger",
    "local_audio_default_model", "local_audio_default_shift",
    "netease_quality", "music_api_timeout", "netease_cookie",
    "netease_meting_base", "netease_api_base", "netease_api_token",
    "alert_admin_on_cookie_expired", "cookie_alert_interval_hours",
    "enable_task_tracking", "preference_storage_path",
    "qq_official_cards", "qq_official_buttons",
}

SENSITIVE_KEYS = {"netease_cookie", "netease_api_token"}

# Gateway /convert 中当前网关明确不生效的参数（页面要如实标注）
IGNORED_BY_GATEWAY = {
    "vocal_vol", "inst_vol", "reverb_intensity", "delay_intensity",
    "uvr5_agg", "uvr5_tta", "uvr5_postprocess", "uvr5_window_size",
    "uvr5_high_end_process", "msst_batch_size", "msst_num_overlap",
    "msst_normalize", "msst_use_tta", "vocal_postprocess",
    "shift_accompaniment", "inst_volume_db", "filter_radius",
}

_ID_RE = re.compile(r"^[0-9a-f]{8,40}$")


def _ok(data: Any = None) -> dict:
    return {"status": "ok", "data": data if data is not None else {}}


def _err(message: str) -> dict:
    return {"status": "error", "message": str(message)}


def _now() -> float:
    return time.time()


def _fmt_bytes(num: int | float | None) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"


def _arg(name: str, default: str = "") -> str:
    """安全读取 query 参数（无请求上下文时返回默认值）。"""
    try:
        value = request.args.get(name)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else str(value)


def _num(payload: dict, key: str, default: float, low: float, high: float):
    """安全取数值：空值/非法值一律回退默认，避免空表单把接口打成 500。"""
    raw = (payload or {}).get(key, None)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return max(low, min(high, value))


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class PageApi:
    """插件页面 API：全部方法在 AstrBot 请求上下文中被调用。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._jobs: dict[str, dict] = {}
        self._models_cache: dict[str, Any] = {"at": 0.0, "payload": None}
        self._load_jobs()

    # ---------------- 注册 ----------------
    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/overview", self.overview, ["GET"], "RVC 控制台 概览"),
            ("/health", self.health, ["GET"], "RVC 控制台 健康检查"),
            ("/tasks", self.tasks, ["GET"], "RVC 控制台 任务列表"),
            ("/task_audio", self.task_audio, ["GET"], "RVC 控制台 任务音频"),
            ("/active_tasks", self.active_tasks, ["GET"], "RVC 控制台 当前任务"),
            ("/models", self.models, ["GET"], "RVC 控制台 模型列表"),
            ("/models/refresh", self.refresh_models, ["POST"], "RVC 控制台 刷新模型"),
            ("/models/default", self.set_default_model, ["POST"], "RVC 控制台 默认音色"),
            ("/models/alias", self.set_alias, ["POST"], "RVC 控制台 模型别名"),
            ("/models/gender", self.set_gender, ["POST"], "RVC 控制台 模型性别"),
            ("/config", self.get_config, ["GET"], "RVC 控制台 配置读取"),
            ("/config/save", self.save_config, ["POST"], "RVC 控制台 配置保存"),
            ("/rvc/sep_models", self.sep_models, ["GET"], "RVC 控制台 分离模型列表"),
            ("/rvc/upload", self.upload_audio, ["POST"], "RVC 控制台 上传音频"),
            ("/rvc/convert", self.start_convert, ["POST"], "RVC 控制台 提交转换"),
            ("/rvc/separate", self.start_separate, ["POST"], "RVC 控制台 提交分离"),
            ("/rvc/clip", self.start_clip, ["POST"], "RVC 控制台 提交切段"),
            ("/rvc/status", self.job_status, ["GET"], "RVC 控制台 转换状态"),
            ("/rvc/result", self.job_result, ["GET"], "RVC 控制台 转换结果"),
            ("/tts/providers", self.tts_providers, ["GET"], "RVC 控制台 TTS 服务商列表"),
            ("/tts/generate", self.tts_generate, ["POST"], "RVC 控制台 TTS 生成"),
            ("/tts/tasks", self.tts_tasks, ["GET"], "RVC 控制台 TTS 任务列表"),
            ("/tts/audio", self.tts_audio, ["GET"], "RVC 控制台 TTS 音频回放"),
            ("/diagnostics", self.diagnostics, ["GET"], "RVC 控制台 诊断"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{API_BASE}{path}", handler, methods, desc)

    # ---------------- 内部工具 ----------------
    @property
    def cfg(self):
        return self.plugin.config

    def _gateway_base(self) -> str:
        return str(self.cfg.get("rvc_base_url", "http://127.0.0.1:3333/") or "").strip()

    def _model_entries(self) -> list[dict]:
        """解析 rvc_models_keywords（"file.pth|||别名"）为结构化条目。"""
        out = []
        for raw in list(self.plugin.rvc_models_keywords or []):
            if not isinstance(raw, str):
                continue
            parts = raw.split("|||", 1)
            filename = parts[0].strip()
            if filename and not filename.lower().endswith(".pth"):
                filename += ".pth"  # 兼容无后缀的历史条目
            alias = parts[1].strip() if len(parts) > 1 else ""
            if not filename:
                continue
            out.append({
                "filename": filename,
                "name": os.path.splitext(filename)[0],
                "alias": alias,
                "display": alias or os.path.splitext(filename)[0],
            })
        return out

    def _gender_of(self, name: str) -> str:
        base = os.path.splitext(str(name))[0].lower()
        try:
            return self.plugin.model_gender_map.get(base, "unknown")
        except Exception:
            return "unknown"

    async def _gateway_model_info(self, use_cache: bool = True) -> dict:
        if use_cache and self._models_cache["payload"] and _now() - self._models_cache["at"] < 30:
            return self._models_cache["payload"]
        base = self._gateway_base()
        payload: dict[str, Any] = {"ok": False, "models": [], "error": "", "dir": ""}
        try:
            async with self.plugin._get_gradio_client(base) as client:  # noqa: SLF001
                raw = await asyncio.wait_for(
                    asyncio.to_thread(client.predict, models_dir="", api_name="/model_info"),
                    timeout=25,
                )
        except Exception as exc:  # noqa: BLE001
            payload["error"] = f"{type(exc).__name__}: {exc}"
            return payload
        models: list[str] = []
        model_dir = ""
        if isinstance(raw, dict):
            model_dir = str(raw.get("dir") or "")
            for item in raw.get("models") or []:
                if isinstance(item, str):
                    models.append(item)
                elif isinstance(item, dict):
                    models.append(str(item.get("name") or item.get("filename") or ""))
        elif isinstance(raw, list):
            models = [str(x) for x in raw if isinstance(x, str)]
        payload.update({
            "ok": True,
            "models": [m for m in models if m],
            "dir": model_dir,
            # 每个模型的 index 匹配情况({模型文件名: 是否有配对 index})
            "index_matches": (raw or {}).get("index_matches", {}) if isinstance(raw, dict) else {},
            "warning": (raw or {}).get("warning", "") if isinstance(raw, dict) else "",
        })
        self._models_cache = {"at": _now(), "payload": payload}
        return payload

    def _load_cover_history(self) -> dict:
        data = _read_json(_data_dir() / "cover_history.json", {})
        return data if isinstance(data, dict) else {}

    def _read_task_records(self, limit: int = 80) -> list[dict]:
        """读 Gateway 落盘的 task 记录（成功任务）。失败任务记录不存在。"""
        if not _task_log_dir(self.plugin).is_dir():
            return []
        files = sorted(
            _task_log_dir(self.plugin).glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        history = self._load_cover_history()
        rows = []
        for path in files:
            rec = _read_json(path, None)
            if not isinstance(rec, dict):
                continue
            task_id = rec.get("task_id") or path.stem
            if str(rec.get("kind") or "") == "tts":
                continue  # CUSTOM-7.4：TTS 任务不入 RVC 历史列表
            # 历史键可能与任务号带后缀（如混响产物 `<task>_reverb`），按前缀匹配
            hist = history.get(task_id) or next(
                (v for k, v in history.items() if k.startswith(task_id + "_")), {})
            row = {
                "id": task_id,
                "source": "gateway",
                "model": rec.get("model") or "",
                "song": Path(str(rec.get("source") or "")).stem or "",
                "artist": "",
                "user": str(hist.get("user") or ""),
                "user_name": str(hist.get("user_name") or ""),
                "pitch": rec.get("pitch"),
                "group_id": str(hist.get("group_id") or ""),
                "group_name": str(hist.get("group_name") or ""),
                "output": rec.get("host_output") or rec.get("output") or "",
                "output_name": Path(str(rec.get("host_output") or rec.get("output") or "")).name,
                "duration": rec.get("output_duration"),
                "requested": rec.get("chorus_seconds_requested"),
                "clip": rec.get("clip_length"),
                "mode": rec.get("render_mode"),
                "created_at": path.stat().st_mtime,
                "status": "succeeded",
                "error": "",
            }
            if hist.get("song"):
                row["song"] = str(hist.get("song"))
                row["artist"] = str(hist.get("artist") or "")
                if hist.get("model"):
                    row["model"] = str(hist.get("model"))
            rows.append(row)
        return rows

    def _jobs_file(self) -> Path:
        return _data_dir() / "webui_jobs.json"

    def _load_jobs(self) -> None:
        data = _read_json(self._jobs_file(), {})
        if isinstance(data, dict):
            self._jobs = {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save_jobs(self) -> None:
        try:
            items = sorted(self._jobs.items(), key=lambda kv: kv[1].get("created_at", 0), reverse=True)
            self._jobs = dict(items[:JOB_KEEP])
            _write_json_atomic(self._jobs_file(), self._jobs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[WebUI] 保存任务记录失败: %s", exc)

    def _job_public(self, job: dict) -> dict:
        data = {k: v for k, v in job.items() if k != "output_path"}
        data["outputs"] = [
            {k: v for k, v in (item or {}).items() if k != "path"}
            for item in (job.get("outputs") or [])
        ]
        data["has_output"] = bool(job.get("output_path") or job.get("outputs"))
        data["elapsed"] = (
            round(_now() - float(job.get("started_at") or job.get("created_at") or _now()), 1)
            if job.get("status") in ("running", "pending") else job.get("elapsed", 0)
        )
        return data

    def _safe_job(self, job_id: str) -> dict | None:
        if not _ID_RE.match(str(job_id or "")):
            return None
        job = self._jobs.get(job_id)
        return job if isinstance(job, dict) else None

    # ---------------- 基础状态 ----------------
    async def health(self) -> dict:
        base = self._gateway_base()
        result = {"ok": False, "base": base, "latency_ms": None, "error": ""}
        if not base:
            result["error"] = "未配置 Gateway 地址"
            return _ok(result)
        started = _now()
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as cli:
                resp = await cli.get(base)
            result["ok"] = resp.status_code < 500
            result["status_code"] = resp.status_code
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_ms"] = int((_now() - started) * 1000)
        return _ok(result)

    async def overview(self) -> dict:
        health = (await self.health())["data"]
        info = await self._gateway_model_info()
        recent_all = self._read_task_records(limit=200)
        tasks = recent_all[:8]
        jobs = sorted(self._jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True)[:6]
        today = time.strftime("%Y-%m-%d")
        today_count = sum(
            1 for t in recent_all
            if time.strftime("%Y-%m-%d", time.localtime(t.get("created_at") or 0)) == today
        )
        default = str(self.cfg.get("rvc_default_model", "") or "")
        alias = ""
        for item in self._model_entries():
            if item["name"] == os.path.splitext(default)[0]:
                alias = item["alias"]
                break
        return _ok({
            "plugin": {"name": PLUGIN_NAME, "loaded": True, "version": self._plugin_version()},
            "gateway": health,
            "models": {
                "gateway_ok": info["ok"],
                "gateway_count": len(info["models"]),
                "configured_count": len(self._model_entries()),
                "dir": info["dir"],
                "error": info["error"],
            },
            "default_model": {"value": default, "alias": alias,
                              "gender": self._gender_of(default)},
            "today": {"count": today_count, "date": today},
            "recent_tasks": tasks,
            "recent_jobs": [self._job_public(j) for j in jobs],
            "paths": {
                "gateway_work": str(_gateway_work(self.plugin) or "(未配置)"),
                "tasks_available": _task_log_dir(self.plugin).is_dir(),
                "outputs_available": _out_dir(self.plugin).is_dir(),
            },
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _plugin_version(self) -> str:
        try:
            meta = Path(__file__).with_name("metadata.yaml").read_text(encoding="utf-8")
            m = re.search(r"^version:\s*(\S+)", meta, re.M)
            return m.group(1) if m else ""
        except Exception:
            return ""

    # ---------------- 任务 ----------------
    async def tasks(self) -> dict:
        try:
            limit = max(1, min(200, int(_arg("limit", "60") or 60)))
        except Exception:
            limit = 60
        rows = self._read_task_records(limit=limit)
        rows += [dict(self._job_public(j), user="WebUI", user_name="WebUI 控制台")
                 for j in self._jobs.values() if j.get("kind") != "tts"]
        rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        return _ok({
            "items": rows[:limit],
            "total": len(rows),
            "sources": {
                "gateway_records": _task_log_dir(self.plugin).is_dir(),
                "webui_jobs": bool(self._jobs),
            },
        })

    def _infer_gateway_stage(self, started_at: float) -> str:
        """按 Gateway tasks 目录里的中间文件推断当前处于哪一步（串行任务取最新目录）。"""
        root = _gateway_work(self.plugin) / "tasks"
        if not root.is_dir():
            return ""
        dirs = [d for d in root.iterdir() if d.is_dir()]
        if not dirs:
            return ""
        latest = max(dirs, key=lambda d: d.stat().st_mtime)
        if started_at and latest.stat().st_mtime < started_at - 5:
            return ""
        sep_dir = latest / "sep"
        if sep_dir.is_dir():
            if list(sep_dir.rglob("*_Vocals.wav")):
                return "变声推理"
            return "人声分离"
        if (latest / "clip.wav").is_file():
            return "准备音源"
        return ""

    async def active_tasks(self) -> dict:
        """当前任务 + 排队情况（来自插件内存队列）。"""
        try:
            snap = self.plugin._queue_snapshot()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return _err(f"读取队列失败: {exc}")
        snap["queue_enabled"] = int(snap.get("limit") or 0) > 0
        # CUSTOM-8.1：注册表里「已接收/排队」但还没进推理槽的任务也纳入排队可见性
        reg_waiting = []
        try:
            queued_ids = {w.get("registry_id") for w in snap.get("waiting") or []}
            active_id = (snap.get("active") or {}).get("registry_id")
            for t in self.plugin.tasks.all_tasks():  # noqa: SLF001
                if t.status not in ("received", "queued"):
                    continue
                if t.task_id in queued_ids or t.task_id == active_id:
                    continue
                reg_waiting.append({
                    "id": t.task_id, "type": t.task_type,
                    "label": t.request_text[:40], "user": t.user_name or t.user_id,
                    "waited": round(_now() - t.created_at, 1),
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug("[WebUI] 注册表等待任务读取失败: %s", exc)
        snap["registry_waiting"] = reg_waiting
        active = snap.get("active")
        if active and active.get("at"):
            stage = self._infer_gateway_stage(float(active.get("at") or 0))
            if stage:
                active["stage"] = stage
                active["step"] = {"准备音源": 1, "人声分离": 2, "变声推理": 3}.get(stage, 2)
                active["stage_from"] = "gateway"
        return _ok(snap)

    async def task_audio(self) -> dict:
        """按任务号返回成品音频（仅允许 gateway out 目录内的文件）。"""
        task_id = _arg("id").strip()
        if not _ID_RE.match(task_id):
            return _err("非法任务号")
        rec = _read_json(_task_log_dir(self.plugin) / f"{task_id}.json", None)
        if not isinstance(rec, dict):
            return _err("找不到该任务记录（可能已被清理）")
        path = Path(str(rec.get("host_output") or rec.get("output") or ""))
        try:
            path.resolve().relative_to(_out_dir(self.plugin).resolve())
        except Exception:
            return _err("结果路径不在允许目录内")
        if not path.is_file():
            return _err("成品文件已被清理")
        size = path.stat().st_size
        if size > MAX_RESULT_BYTES:
            return _err(f"结果过大（{_fmt_bytes(size)}），请到服务器查看")
        return _ok({
            "job_id": task_id,
            "name": path.name,
            "size": size,
            "size_text": _fmt_bytes(size),
            "duration": rec.get("output_duration"),
            "mime": "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg",
            "audio_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        })

    # ---------------- 模型 ----------------
    async def models(self) -> dict:
        info = await self._gateway_model_info(use_cache=False)
        configured = {m["filename"]: m for m in self._model_entries()}
        rows = []
        idx_matches = info.get("index_matches") or {}
        for name in info["models"]:
            base = os.path.splitext(name)[0]
            hit = configured.get(name) or configured.get(base + ".pth")
            rows.append({
                "filename": name,
                "name": base,
                "alias": (hit or {}).get("alias", ""),
                "gender": self._gender_of(name),
                "in_config": bool(hit),
                "has_index": idx_matches.get(name),
            })
        for filename, item in configured.items():
            if not any(r["filename"] == filename for r in rows):
                rows.append({
                    "filename": filename,
                    "name": item["name"],
                    "alias": item["alias"],
                    "gender": self._gender_of(filename),
                    "in_config": True,
                    "missing_in_gateway": True,
                })
        default = str(self.cfg.get("rvc_default_model", "") or "")
        return _ok({
            "gateway_ok": info["ok"],
            "gateway_error": info["error"],
            "dir": info["dir"],
            "warning": info.get("warning", ""),
            "models": rows,
            "default": default,
            "default_missing": bool(default) and not any(
                os.path.splitext(r["filename"])[0] == os.path.splitext(default)[0] or
                r["filename"] == default for r in rows),
        })

    async def refresh_models(self) -> dict:
        try:
            result = await self.plugin._update_models_from_api()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return _err(f"刷新失败: {exc}")
        self._models_cache = {"at": 0.0, "payload": None}
        if not isinstance(result, dict):
            return _err("刷新返回格式异常")
        if not result.get("ok"):
            return _err(result.get("reason") or "刷新失败")
        return _ok({
            "models": self._model_entries(),
            "missing_default": result.get("missing_default", ""),
            "default": str(self.cfg.get("rvc_default_model", "") or ""),
        })

    async def set_default_model(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        name = str(payload.get("model") or "").strip()
        if not name:
            return _err("缺少 model")
        entries = {e["name"]: e for e in self._model_entries()}
        key = os.path.splitext(name)[0]
        if key not in entries:
            return _err(f"模型 {name} 不在已配置列表中，先刷新模型")
        self.cfg["rvc_default_model"] = key
        self.cfg.save_config()
        try:
            self.plugin.rvc_default_model = key
        except Exception:
            pass
        logger.info("[WebUI] 默认音色改为 %s", key)
        return _ok({"default": key})

    async def set_alias(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        model = str(payload.get("model") or "").strip()
        alias = str(payload.get("alias") or "").strip()[:40]
        if not model:
            return _err("缺少 model")
        key = os.path.splitext(model)[0]
        items = list(self.plugin.rvc_models_keywords or [])
        found = False
        for i, raw in enumerate(items):
            if not isinstance(raw, str):
                continue
            parts = raw.split("|||", 1)
            if os.path.splitext(parts[0].strip())[0] == key:
                items[i] = f"{parts[0].strip()}|||{alias}"
                found = True
                break
        if not found:
            items.append(f"{key}.pth|||{alias}")
        self.plugin.rvc_models_keywords = items
        self.cfg["rvc_models_keywords"] = items
        self.cfg.save_config()
        return _ok({"model": key, "alias": alias})

    async def set_gender(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        model = str(payload.get("model") or "").strip()
        gender = str(payload.get("gender") or "").strip().lower()
        if not model:
            return _err("缺少 model")
        if gender not in ("male", "female", "unknown"):
            return _err("gender 只能是 male/female/unknown")
        key = os.path.splitext(model)[0]
        try:
            self.plugin._write_model_gender(key, gender)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return _err(f"写入失败: {exc}")
        return _ok({"model": key, "gender": gender})

    # ---------------- 配置 ----------------
    def _schema(self) -> dict:
        path = Path(__file__).with_name("_conf_schema.json")
        data = _read_json(path, {})
        return data if isinstance(data, dict) else {}

    def _flatten_schema(self, schema: dict, prefix: str = "", group: str = "") -> list[dict]:
        rows = []
        for key, meta in schema.items():
            if not isinstance(meta, dict):
                continue
            path = f"{prefix}{key}"
            mtype = meta.get("type")
            if mtype == "object" and isinstance(meta.get("items"), dict):
                rows.extend(self._flatten_schema(meta["items"], path + ".", str(meta.get("description") or key)))
                continue
            rows.append({
                "key": path,
                "group": group,
                "type": mtype or "string",
                "label": meta.get("description") or key,
                "hint": meta.get("hint") or "",
                "default": meta.get("default"),
                "options": meta.get("options"),
                "minimum": meta.get("minimum"),
                "maximum": meta.get("maximum"),
                "invisible": bool(meta.get("invisible")),
                "sensitive": key in SENSITIVE_KEYS,
            })
        return rows

    def _get_cfg(self, key: str, default=None):
        try:
            return self.cfg.get(key, default)
        except Exception:
            return default

    async def get_config(self) -> dict:
        fields = [f for f in self._flatten_schema(self._schema()) if not f["invisible"]]
        values = {}
        for f in fields:
            if f["sensitive"]:
                values[f["key"]] = ""
                continue
            values[f["key"]] = self._get_cfg(f["key"], f.get("default"))
        return _ok({
            "fields": fields,
            "values": values,
            "sensitive_configured": {
                k: bool(str(self._get_cfg(k, "") or "").strip()) for k in SENSITIVE_KEYS
            },
            "reload_required": sorted(RELOAD_REQUIRED),
            "gateway_ignored": sorted(IGNORED_BY_GATEWAY),
        })

    async def save_config(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        values = payload.get("values") or {}
        if not isinstance(values, dict):
            return _err("values 必须是对象")
        fields = {f["key"]: f for f in self._flatten_schema(self._schema()) if not f["invisible"]}
        # 对象节点键（如 tuning.gender.model_genders，由音色列表编辑器整组提交）
        node_paths = set()

        def _collect_nodes(schema, prefix=""):
            for key, meta in schema.items():
                if not isinstance(meta, dict):
                    continue
                path = f"{prefix}{key}"
                if meta.get("type") == "object" and isinstance(meta.get("items"), dict):
                    node_paths.add(path)
                    _collect_nodes(meta["items"], path + ".")
                elif meta.get("type") in ("dict", "object"):
                    # dict/object 叶子（如 model_genders_extra）也按整组提交
                    node_paths.add(path)
        _collect_nodes(self._schema())

        changed, needs_reload, errors = [], [], []
        old_values = {}
        for key, raw in values.items():
            meta = fields.get(key)
            if meta is None:
                if key in node_paths:
                    try:
                        value = raw if isinstance(raw, dict) else json.loads(str(raw))
                    except Exception:  # noqa: BLE001
                        errors.append(f"{key}: 值必须是对象")
                        continue
                    old = self._get_cfg(key)
                    if old == value:
                        continue
                    old_values[key] = old
                    try:
                        self.cfg[key] = value
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{key}: 写入失败 {exc}")
                        continue
                    changed.append(key)
                    continue
                errors.append(f"{key}: 未知配置项")
                continue
            if meta["sensitive"] and not str(raw or "").strip():
                continue  # 留空表示不修改
            try:
                value = self._coerce(meta, raw)
            except ValueError as exc:
                errors.append(f"{meta['label']}: {exc}")
                continue
            old = self._get_cfg(key, meta.get("default"))
            if old == value:
                continue
            old_values[key] = old
            try:
                self.cfg[key] = value
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{meta['label']}: 写入失败 {exc}")
                continue
            changed.append(key)
            if key in RELOAD_REQUIRED:
                needs_reload.append(key)
        if errors and not changed:
            return _err("；".join(errors[:5]))
        if changed:
            try:
                self.cfg.save_config()
            except Exception as exc:  # noqa: BLE001
                return _err(f"保存失败: {exc}")
            try:
                self.plugin.apply_live_config(changed)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                logger.debug("[WebUI] 运行时同步失败: %s", exc)
            logger.info("[WebUI] 配置已更新: %s", ", ".join(changed))
        return _ok({
            "changed": changed,
            "old": old_values,
            "needs_reload": sorted(set(needs_reload)),
            "errors": errors,
        })

    def _coerce(self, meta: dict, raw):
        mtype = meta["type"]
        if mtype == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if mtype == "int":
            value = int(float(raw))
            lo, hi = meta.get("minimum"), meta.get("maximum")
            if lo is not None and value < lo:
                raise ValueError(f"不能小于 {lo}")
            if hi is not None and value > hi:
                raise ValueError(f"不能大于 {hi}")
            return value
        if mtype == "float":
            value = float(raw)
            lo, hi = meta.get("minimum"), meta.get("maximum")
            if lo is not None and value < lo:
                raise ValueError(f"不能小于 {lo}")
            if hi is not None and value > hi:
                raise ValueError(f"不能大于 {hi}")
            return value
        if mtype == "list":
            if isinstance(raw, list):
                return [str(x) for x in raw]
            text = str(raw).replace("，", ",")
            return [x.strip() for x in re.split(r"[,\n]", text) if x.strip()]
        if mtype == "object":
            if isinstance(raw, dict):
                return raw
            return json.loads(str(raw))
        if mtype == "dict":
            if isinstance(raw, dict):
                return raw
            return json.loads(str(raw)) if str(raw).strip() else {}
        text = str(raw)
        options = meta.get("options")
        if options and text not in options:
            raise ValueError(f"只能是 {'/'.join(map(str, options))}")
        return text

    # ---------------- RVC 控制台 ----------------
    async def upload_audio(self) -> dict:
        """接收音频：优先 multipart，其次 JSON base64（bridge 只支持 JSON）。"""
        filename = ""
        size = 0
        upload_id = uuid.uuid4().hex[:24]
        dest = None

        upload = None
        try:
            files = request.files
            upload = files.get("file") if hasattr(files, "get") else None
        except Exception:
            upload = None

        if upload is not None:
            filename = str(getattr(upload, "filename", "") or "")
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_AUDIO_EXT:
                return _err(f"不支持的格式 {ext or '(无扩展名)'}，仅支持 {', '.join(sorted(ALLOWED_AUDIO_EXT))}")
            _upload_dir(self.plugin).mkdir(parents=True, exist_ok=True)
            dest = _upload_dir(self.plugin) / f"{upload_id}{ext}"
            try:
                size = await asyncio.wait_for(
                    upload.save(str(dest), max_bytes=MAX_UPLOAD_BYTES), timeout=120)
            except Exception as exc:  # noqa: BLE001
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                return _err(f"保存失败: {exc}")
        else:
            payload = await request.get_json(silent=True) or {}
            filename = str(payload.get("name") or "").strip()
            b64 = str(payload.get("data_base64") or "").strip()
            if not filename or not b64:
                return _err("缺少文件数据（name / data_base64）")
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_AUDIO_EXT:
                return _err(f"不支持的格式 {ext or '(无扩展名)'}，仅支持 {', '.join(sorted(ALLOWED_AUDIO_EXT))}")
            try:
                raw_bytes = base64.b64decode(b64, validate=True)
            except Exception:
                return _err("base64 数据无法解码")
            if len(raw_bytes) > MAX_UPLOAD_BYTES:
                return _err(f"文件过大（上限 {_fmt_bytes(MAX_UPLOAD_BYTES)}）")
            _upload_dir(self.plugin).mkdir(parents=True, exist_ok=True)
            dest = _upload_dir(self.plugin) / f"{upload_id}{ext}"
            dest.write_bytes(raw_bytes)
            size = len(raw_bytes)

        self._prune_uploads()
        return _ok({
            "upload_id": upload_id,
            "name": filename,
            "size": size,
            "size_text": _fmt_bytes(size),
            "container_path": f"/work/uploads/{upload_id}{ext}",
        })

    def _prune_uploads(self) -> None:
        try:
            items = sorted(_upload_dir(self.plugin).glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in items[UPLOAD_KEEP:]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    async def sep_models(self) -> dict:
        """读取网关可用分离模型（/show_msst_models）。"""
        try:
            base = self._gateway_base()
            async with self.plugin._get_gradio_client(base) as client:  # noqa: SLF001
                raw = await asyncio.wait_for(
                    asyncio.to_thread(client.predict, api_name="/show_msst_models"),
                    timeout=20,
                )
        except Exception as exc:  # noqa: BLE001
            return _err(f"读取分离模型失败: {exc}")
        models = []
        for item in raw or []:
            if isinstance(item, dict) and item.get("id"):
                models.append({"id": item["id"], "name": item.get("name") or item["id"]})
            elif isinstance(item, str):
                models.append({"id": item, "name": os.path.splitext(item)[0]})
        return _ok({"models": models, "default": models[0]["id"] if models else ""})

    async def start_separate(self) -> dict:
        """提交仅分离任务（人声/伴奏），不跑 RVC。"""
        payload = await request.get_json(silent=True) or {}
        upload_id = str(payload.get("upload_id") or "").strip()
        if not _ID_RE.match(upload_id):
            return _err("缺少或非法的 upload_id")
        matches = list(_upload_dir(self.plugin).glob(f"{upload_id}.*")) if _upload_dir(self.plugin).is_dir() else []
        if not matches:
            return _err("上传文件已失效，请重新上传")
        src = matches[0]
        mode = str(payload.get("mode") or "both").strip().lower()
        if mode not in ("both", "vocals", "instrumental"):
            return _err("处理方式只能是 both / vocals / instrumental")
        fmt = str(payload.get("output_format") or "wav").strip().lower()
        if fmt not in ("wav", "flac", "mp3", "m4a"):
            return _err("不支持的导出格式")
        running = [j for j in self._jobs.values() if j.get("status") in ("pending", "running")]
        if running:
            return _err(f"已有任务进行中（{running[0]['id'][:8]}），请等待完成")
        job_id = uuid.uuid4().hex[:16]
        params = {
            "song_name_src": f"/work/uploads/{src.name}",
            "msst_model": str(payload.get("model") or "").strip(),
            "output_format": fmt,
            "mode": mode,
        }
        job = {
            "id": job_id,
            "kind": "separate",
            "created_at": _now(),
            "started_at": _now(),
            "updated_at": _now(),
            "status": "running",
            "stage": "已提交到 Gateway",
            "model": params["msst_model"] or "default",
            "input_name": src.name,
            "mode": mode,
            "format": fmt,
            "output_path": "",
            "error": "",
        }
        self._jobs[job_id] = job
        self._save_jobs()
        asyncio.create_task(self._run_job(job_id, params))
        return _ok({"job_id": job_id})

    async def start_clip(self) -> dict:
        """提交切段任务：只裁出目标时长的片段，不做分离/变声。"""
        payload = await request.get_json(silent=True) or {}
        upload_id = str(payload.get("upload_id") or "").strip()
        if not _ID_RE.match(upload_id):
            return _err("缺少或非法的 upload_id")
        matches = list(_upload_dir(self.plugin).glob(f"{upload_id}.*")) if _upload_dir(self.plugin).is_dir() else []
        if not matches:
            return _err("上传文件已失效，请重新上传")
        src = matches[0]
        fmt = str(payload.get("output_format") or "wav").strip().lower()
        if fmt not in ("wav", "flac", "mp3", "m4a"):
            fmt = "wav"
        seconds = int(_num(payload, "seconds", 25, 10, 200))
        running = [j for j in self._jobs.values() if j.get("status") in ("pending", "running")]
        if running:
            return _err(f"已有任务进行中（{running[0]['id'][:8]}），请等待完成")
        job_id = uuid.uuid4().hex[:16]
        params = {
            "song_name_src": f"/work/uploads/{src.name}",
            "seconds": seconds,
            "output_format": fmt,
        }
        job = {
            "id": job_id, "kind": "clip",
            "created_at": _now(), "started_at": _now(), "updated_at": _now(),
            "status": "running", "stage": "已提交到 Gateway",
            "model": "-", "input_name": src.name,
            "seconds": seconds, "format": fmt,
            "output_path": "", "error": "",
        }
        self._jobs[job_id] = job
        self._save_jobs()
        asyncio.create_task(self._run_job(job_id, params))
        return _ok({"job_id": job_id})

    async def start_convert(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        upload_id = str(payload.get("upload_id") or "").strip()
        if not _ID_RE.match(upload_id):
            return _err("缺少或非法的 upload_id")
        matches = list(_upload_dir(self.plugin).glob(f"{upload_id}.*")) if _upload_dir(self.plugin).is_dir() else []
        if not matches:
            return _err("上传文件已失效，请重新上传")
        src = matches[0]
        model = str(payload.get("model") or "").strip()
        entries = {e["name"]: e for e in self._model_entries()}
        if not model or os.path.splitext(model)[0] not in entries:
            return _err("请选择已配置的音色")
        model = entries[os.path.splitext(model)[0]]["filename"]

        running = [j for j in self._jobs.values() if j.get("status") in ("pending", "running")]
        if running:
            return _err(f"已有转换进行中（{running[0]['id'][:8]}），请等待完成")

        fmt = str(payload.get("output_format") or "wav").strip().lower()
        if fmt not in ("wav", "mp3", "flac", "m4a"):
            fmt = "wav"
        rmode = str(payload.get("render_mode") or "chorus").strip().lower()
        if rmode not in ("chorus", "custom"):
            rmode = "chorus"
        f0 = str(payload.get("f0_method") or "rmvpe").strip() or "rmvpe"
        params = {
            "song_name_src": f"/work/uploads/{src.name}",
            "model_dropdown": model,
            # Gateway 的 index_rate 是 Textbox(str)，必须按字符串传
            "key_shift": int(_num(payload, "key_shift", 0, -24, 24)),
            "f0_method": f0,
            "index_rate": str(round(_num(payload, "index_rate", 0.75, 0.0, 1.0), 2)),
            "rms_mix_rate": round(_num(payload, "rms_mix_rate", 0.35, 0.0, 1.0), 2),
            "protect": round(_num(payload, "protect", 0.33, 0.0, 0.5), 2),
            "vocal_volume_db": round(_num(payload, "vocal_volume_db", 0.0, -12.0, 12.0), 1),
            "output_format": fmt,
            "render_mode": rmode,
            "chorus_seconds": int(_num(payload, "chorus_seconds", 20, 10, 200)),
            "fade_seconds": round(_num(payload, "fade_seconds", 0.5, 0.0, 5.0), 2),
            "speaker_id": int(_num(payload, "speaker_id", 0, 0, 20)),
            "vocal_vol": 0,
            "inst_vol": 0,
            "shift_accompaniment": False,
        }
        for key in ("start_time", "end_time", "duration"):
            value = str(payload.get(key) or "").strip()
            if value:
                params[key] = value
        # CUSTOM-7：本次转换的人声混响试听参数（只对本次请求生效，不写配置）
        if payload.get("reverb_enabled") in (True, "true", "1", "on"):
            params.update({
                "reverb_enabled": True,
                "reverb_wet": round(_num(payload, "reverb_wet", 0.18, 0.0, 0.5), 2),
                "reverb_dry": round(_num(payload, "reverb_dry", 1.0, 0.5, 1.0), 2),
                "reverb_room_size": round(_num(payload, "reverb_room_size", 0.65, 0.0, 1.0), 2),
                "reverb_decay": round(_num(payload, "reverb_decay", 1.4, 0.2, 4.0), 2),
                "reverb_damping": round(_num(payload, "reverb_damping", 0.45, 0.0, 1.0), 2),
                "reverb_pre_delay": round(_num(payload, "reverb_pre_delay", 18.0, 0.0, 100.0), 1),
                "reverb_width": round(_num(payload, "reverb_width", 0.8, 0.0, 1.0), 2),
            })
        if params["key_shift"] < -24 or params["key_shift"] > 24:
            return _err("变调范围只能是 -24 ~ +24")
        if not 0 <= params["index_rate"] <= 1:
            return _err("index_rate 必须在 0~1")
        if not 0 <= params["protect"] <= 0.5:
            return _err("protect 必须在 0~0.5")

        job_id = uuid.uuid4().hex[:16]
        job = {
            "id": job_id,
            "kind": "convert",
            "created_at": _now(),
            "started_at": _now(),
            "updated_at": _now(),
            "status": "running",
            "stage": "已提交到 Gateway",
            "model": os.path.splitext(model)[0],
            "input_name": src.name,
            "params": {k: v for k, v in params.items() if k not in ("song_name_src",)},
            "output_path": "",
            "error": "",
        }
        self._jobs[job_id] = job
        self._save_jobs()
        asyncio.create_task(self._run_job(job_id, params))
        return _ok({"job_id": job_id})

    # ---------------- TTS 控制台（CUSTOM-7） ----------------
    @staticmethod
    def _provider_id(provider: Any) -> str:
        try:
            meta = provider.meta() if hasattr(provider, "meta") else None
        except Exception:
            meta = None
        if isinstance(meta, dict):
            return str(meta.get("id") or "")
        return str(getattr(meta, "id", "") or "")

    async def tts_providers(self) -> dict:
        """列出 AstrBot 已配置的 TTS 服务商（供控制台下拉，不暴露任何 Key）。"""
        pm = getattr(self.plugin.context, "provider_manager", None)
        rows = []
        for p in getattr(pm, "tts_provider_insts", None) or []:
            pid = self._provider_id(p)
            rows.append({
                "id": pid,
                "class": type(p).__name__,
                "model": str(getattr(p, "model_name", "") or getattr(p, "voice", "") or ""),
            })
        using = ""
        try:
            from astrbot.core.provider.entities import ProviderType

            current = pm.get_using_provider(ProviderType.TEXT_TO_SPEECH) if pm else None
            using = self._provider_id(current) if current else ""
        except Exception:
            pass
        return _ok({
            "providers": rows,
            "configured": str(self.cfg.get("tts_provider_name", "") or ""),
            "using": using,
            "tts_enable": bool(self.cfg.get("tts_enable", False)),
            "tts_model": str(self.cfg.get("tts_rvc_model", "") or ""),
            "gateway_models": [m["filename"] for m in self._model_entries()],
        })

    async def tts_generate(self) -> dict:
        """提交 TTS 生成任务：文本 → TTS Provider → 网关换声 → 原始/转换双预览。"""
        payload = await request.get_json(silent=True) or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return _err("请输入要朗读的文本")
        try:
            max_chars = int(self.cfg.get("tts_max_text_chars", 200) or 200)
        except (TypeError, ValueError):
            max_chars = 200
        text = text[:min(max_chars, 500)]
        provider_id = str(payload.get("provider_id") or "").strip()[:80]
        model = str(payload.get("model") or "").strip()
        if model:
            entries = {os.path.splitext(e["filename"])[0] for e in self._model_entries()}
            model = model if model.endswith(".pth") else f"{model}.pth"
            if os.path.splitext(model)[0] not in entries:
                return _err(f"音色 {model} 不在已配置模型列表中")
        reverb_raw = payload.get("reverb")
        reverb_override = None if reverb_raw in (None, "", "default") else bool(reverb_raw)
        pitch = int(_num(payload, "pitch", 0, -24, 24))
        fmt = str(payload.get("output_format") or "").strip().lower()
        if fmt and fmt not in ("wav", "mp3", "flac", "m4a"):
            return _err("不支持的输出格式")
        fmt_override = fmt or ""

        running = [j for j in self._jobs.values() if j.get("status") in ("pending", "running")]
        if running:
            return _err(f"已有任务进行中（{running[0]['id'][:8]}），请等待完成")

        job_id = uuid.uuid4().hex[:16]
        job = {
            "id": job_id,
            "kind": "tts",
            "created_at": _now(),
            "started_at": _now(),
            "updated_at": _now(),
            "status": "running",
            "stage": "TTS 生成原始语音",
            "model": os.path.splitext(model)[0] if model else str(self.cfg.get("tts_rvc_model", "") or "") or "default",
            "text": text[:120] + ("…" if len(text) > 120 else ""),
            "output_path": "",
            "error": "",
        }
        self._jobs[job_id] = job
        self._save_jobs()
        asyncio.create_task(self._run_tts_job(job_id, {
            "text": text,
            "provider_id": provider_id,
            "model": model,
            "reverb_override": reverb_override,
            "pitch": pitch,
            "output_format": fmt_override,
        }))
        return _ok({"job_id": job_id})

    async def _run_tts_job(self, job_id: str, params: dict) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        started = _now()
        try:
            tts = getattr(self.plugin, "tts", None)
            if tts is None:
                raise RuntimeError("TTS 模块未初始化")
            raw_path, out_path, info = await asyncio.wait_for(
                tts.convert(
                    params["text"], None,
                    provider_id=params.get("provider_id") or "",
                    model=params.get("model") or "",
                    reverb_override=params.get("reverb_override"),
                    pitch_override=params.get("pitch"),
                    format_override=params.get("output_format") or "",
                ),
                timeout=max(120, float(getattr(self.plugin, "tts_timeout", 300) or 300) + 60),
            )
            # 原始音频复制到成品目录供页面回放（保持与 /rvc/result 同一安全边界）
            outputs = []
            try:
                raw_name = f"{job_id}_tts_raw{os.path.splitext(raw_path)[1].lower() or '.wav'}"
                raw_copy = _out_dir(self.plugin) / raw_name
                shutil.copyfile(raw_path, raw_copy)
                os.chmod(raw_copy, 0o644)
                outputs.append({
                    "label": "raw", "label_text": "原始 TTS",
                    "path": str(raw_copy), "name": raw_copy.name,
                    "size": raw_copy.stat().st_size,
                    "duration": self._probe_duration(str(raw_copy)),
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("[WebUI] 原始 TTS 音频复制失败（只影响预览）: %s", exc)
            outputs.append({
                "label": "output", "label_text": "RVC 转换后",
                "path": out_path, "name": Path(out_path).name,
                "size": Path(out_path).stat().st_size,
                "duration": info.get("output_duration") or self._probe_duration(out_path),
            })
            job["output_path"] = out_path
            job["output_name"] = Path(out_path).name
            job["outputs"] = outputs
            job["status"] = "succeeded"
            job["stage"] = "完成"
            job["model_used"] = info.get("model") or ""
            job["reverb"] = info.get("reverb") or {}
            # 控制台任务也进 TTS 历史（触发用户标为 WebUI，文本即朗读内容）
            try:
                self.plugin._record_tts_history(  # noqa: SLF001
                    task_id=job_id,
                    user="WebUI",
                    user_name="WebUI 控制台",
                    request_text=params["text"],
                    speak_text=params["text"],
                    model=str(info.get("model") or ""),
                    output_path=out_path,
                    duration=info.get("output_duration"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[WebUI] 记录 TTS 历史失败: %s", exc)
        except asyncio.TimeoutError:
            job["status"] = "failed"
            job["error"] = "TTS 转换超时"
            job["stage"] = "超时"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = str(exc)
            job["stage"] = "失败"
            logger.warning("[WebUI] TTS 生成失败 %s: %s", job_id, exc)
        finally:
            job["elapsed"] = round(_now() - started, 1)
            job["updated_at"] = _now()
            self._save_jobs()

    async def _run_job(self, job_id: str, params: dict) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        started = _now()
        kind = job.get("kind") or "convert"
        timeout = float(self.cfg.get("inference_timeout", 600) or 600) + 60
        try:
            base = self._gateway_base()
            async with self.plugin._get_gradio_client(base) as client:  # noqa: SLF001
                job["stage"] = ("Gateway 正在分离人声/伴奏（CPU，约 1-2 分钟）"
                                if kind == "separate"
                                else "Gateway 正在分离与推理（CPU，可能数分钟）")
                job["updated_at"] = _now()
                if kind == "separate":
                    raw = await asyncio.wait_for(
                        asyncio.to_thread(client.predict, **params, api_name="/separate"),
                        timeout=timeout,
                    )
                    self._finish_separate(job, raw)
                elif kind == "clip":
                    raw = await asyncio.wait_for(
                        asyncio.to_thread(client.predict, **params, api_name="/clip"),
                        timeout=timeout,
                    )
                    self._finish_clip(job, raw)
                else:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(client.predict, **params, api_name="/convert"),
                        timeout=timeout,
                    )
                    self._finish_convert(job, result)
        except asyncio.TimeoutError:
            job["status"] = "failed"
            job["error"] = "推理超时"
            job["stage"] = "超时"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["stage"] = "失败"
            logger.warning("[WebUI] 转换失败 %s: %s", job_id, exc)
        finally:
            job["elapsed"] = round(_now() - started, 1)
            job["updated_at"] = _now()
            self._save_jobs()

    def _finish_convert(self, job: dict, result) -> None:
        path = ""
        if isinstance(result, (list, tuple)) and result:
            path = str(result[0] or "")
        elif isinstance(result, str):
            path = result
        if not path or not Path(path).is_file():
            job["status"] = "failed"
            job["error"] = f"Gateway 未返回有效文件: {path or '空'}"
            job["stage"] = "失败"
            return
        job["output_path"] = path
        job["output_name"] = Path(path).name
        job["status"] = "succeeded"
        job["stage"] = "完成"
        job["size"] = Path(path).stat().st_size
        job["duration"] = self._probe_duration(path)
        job["outputs"] = [{
            "label": "output", "label_text": "翻唱成品",
            "path": path, "name": Path(path).name,
            "size": job["size"], "duration": job["duration"],
        }]

    def _finish_clip(self, job: dict, raw) -> None:
        data = raw if isinstance(raw, dict) else {}
        item = data.get("file")
        if not isinstance(item, dict):
            job["status"] = "failed"
            job["error"] = "切段未返回有效文件"
            job["stage"] = "失败"
            return
        path = str(item.get("host_path") or "")
        if not path or not Path(path).is_file():
            job["status"] = "failed"
            job["error"] = f"切段结果不存在: {path or '空'}"
            job["stage"] = "失败"
            return
        job["outputs"] = [{
            "label": "clip", "label_text": "切段结果", "path": path,
            "name": item.get("name") or Path(path).name,
            "size": item.get("size") or Path(path).stat().st_size,
            "duration": item.get("duration"),
        }]
        job["status"] = "succeeded"
        job["stage"] = "完成"
        job["clip_range"] = [data.get("start"), data.get("length")]
        job["clip_source"] = data.get("clip_source")

    def _finish_separate(self, job: dict, raw) -> None:
        data = raw if isinstance(raw, dict) else {}
        outputs = []
        for label, text in (("vocals", "人声"), ("instrumental", "伴奏")):
            item = data.get(label)
            if not isinstance(item, dict):
                continue
            path = str(item.get("host_path") or "")
            if not path or not Path(path).is_file():
                continue
            outputs.append({
                "label": label, "label_text": text, "path": path,
                "name": item.get("name") or Path(path).name,
                "size": item.get("size") or Path(path).stat().st_size,
                "duration": item.get("duration"),
            })
        if not outputs:
            job["status"] = "failed"
            job["error"] = "分离未返回有效文件"
            job["stage"] = "失败"
            return
        job["outputs"] = outputs
        job["status"] = "succeeded"
        job["stage"] = "完成"
        job["sep_model"] = data.get("sep_model")
        job["format"] = data.get("format")

    @staticmethod
    def _probe_duration(path: str) -> float | None:
        import subprocess

        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            return round(float(out), 2) if out else None
        except Exception:
            return None

    async def job_status(self) -> dict:
        job_id = _arg("job_id").strip()
        job = self._safe_job(job_id)
        if job is None:
            if _ID_RE.match(job_id):
                return _ok({"id": job_id, "status": "unknown", "stage": "记录已过期"})
            return _err("非法 job_id")
        return _ok(self._job_public(job))

    async def job_result(self) -> dict:
        job_id = _arg("job_id").strip()
        job = self._safe_job(job_id)
        if job is None:
            return _err("任务不存在或记录已过期")
        if job.get("status") != "succeeded":
            return _err("任务尚未成功完成")
        outputs = list(job.get("outputs") or [])
        if not outputs and job.get("output_path"):
            outputs = [{
                "label": "output", "label_text": "翻唱成品",
                "path": job.get("output_path"), "name": job.get("output_name"),
                "size": job.get("size"), "duration": job.get("duration"),
            }]
        files = []
        for item in outputs:
            path = Path(str(item.get("path") or ""))
            try:
                path.resolve().relative_to(_out_dir(self.plugin).resolve())
            except Exception:
                continue
            if not path.is_file():
                continue
            size = path.stat().st_size
            entry = {
                "label": item.get("label") or "output",
                "label_text": item.get("label_text") or "输出",
                "name": path.name,
                "size": size,
                "size_text": _fmt_bytes(size),
                "duration": item.get("duration"),
                "mime": "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg",
            }
            if size > MAX_RESULT_BYTES:
                entry["error"] = f"结果过大（{_fmt_bytes(size)}），请到服务器查看"
            else:
                entry["audio_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
            files.append(entry)
        if not files:
            return _err("结果文件已被清理或在允许目录之外")
        return _ok({"job_id": job_id, "kind": job.get("kind") or "convert", "outputs": files})

    def _load_tts_history(self) -> dict:
        data = _read_json(_data_dir() / "tts_history.json", {})
        return data if isinstance(data, dict) else {}

    async def tts_tasks(self) -> dict:
        """TTS 当前队列 + 历史任务（含触发用户与文本，列表里文本过长会截断）。"""
        try:
            snap = self.plugin._tts_queue_snapshot()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return _err(f"读取队列失败: {exc}")
        rows = []
        for item in self._load_tts_history().values():
            path = str(item.get("output") or "")
            playable = bool(path) and Path(path).resolve().as_posix().startswith(
                _out_dir(self.plugin).resolve().as_posix()) and Path(path).is_file()
            rows.append({
                "id": str(item.get("task_id") or ""),
                "user": str(item.get("user") or ""),
                "user_name": str(item.get("user_name") or ""),
                "group_id": str(item.get("group_id") or ""),
                "group_name": str(item.get("group_name") or ""),
                "request": str(item.get("request") or "")[:60],
                "speak": str(item.get("speak") or "")[:60],
                "model": str(item.get("model") or ""),
                "output_name": Path(path).name if path else "",
                "duration": item.get("duration"),
                "created_at": item.get("created_at") or 0,
                "status": "succeeded",
                "playable": playable,
            })
        rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        try:
            limit = max(1, min(200, int(_arg("limit", "60") or 60)))
        except Exception:  # noqa: BLE001
            limit = 60
        return _ok({
            "active": snap.get("active"),
            "waiting": snap.get("waiting") or [],
            "queue_limit": snap.get("limit", 3),
            "items": rows[:limit],
            "total": len(rows),
        })

    async def tts_audio(self) -> dict:
        """按任务号返回 TTS 音频（仅允许网关 out 目录内的成品）。"""
        task_id = _arg("id").strip()
        if not _ID_RE.match(task_id):
            return _err("非法任务号")
        item = self._load_tts_history().get(task_id)
        if not isinstance(item, dict):
            return _err("找不到该 TTS 任务记录")
        path = Path(str(item.get("output") or ""))
        try:
            path.resolve().relative_to(_out_dir(self.plugin).resolve())
        except Exception:  # noqa: BLE001
            return _err("结果路径不在允许目录内")
        if not path.is_file():
            return _err("音频已被清理")
        size = path.stat().st_size
        if size > MAX_RESULT_BYTES:
            return _err(f"音频过大（{_fmt_bytes(size)}），请到服务器查看")
        return _ok({
            "id": task_id,
            "name": path.name,
            "speak": str(item.get("speak") or ""),
            "user": str(item.get("user") or ""),
            "group_id": str(item.get("group_id") or ""),
            "group_name": str(item.get("group_name") or ""),
            "duration": item.get("duration"),
            "mime": "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg",
            "audio_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        })

    # ---------------- 诊断 ----------------
    async def diagnostics(self) -> dict:
        health = (await self.health())["data"]
        info = await self._gateway_model_info(use_cache=False)
        entries = self._model_entries()
        default = str(self.cfg.get("rvc_default_model", "") or "")
        default_key = os.path.splitext(default)[0]
        checks = []

        def add(name: str, ok: bool, detail: str, level: str = "error"):
            checks.append({"name": name, "ok": ok, "detail": detail, "level": level})

        add("Gateway 连通", bool(health.get("ok")),
            f"{health.get('base')} · {health.get('latency_ms')}ms" if health.get("ok")
            else (health.get("error") or "不可达"))
        add("Gateway 模型列表", bool(info["ok"]), info["error"] or f"{len(info['models'])} 个模型")
        add("插件已配置音色", bool(entries), f"{len(entries)} 个")
        add("默认音色已设置", bool(default), default or "未设置（会弹音色菜单）", "warning")
        add("默认音色在配置列表", bool(default_key) and any(e["name"] == default_key for e in entries),
            default_key or "-")
        if info["ok"] and default_key:
            add("默认音色在 Gateway 列表",
                any(os.path.splitext(m)[0] == default_key for m in info["models"]),
                default_key)
        add("默认音色性别已知", self._gender_of(default) in ("male", "female"),
            self._gender_of(default), "warning")
        try:
            timeout = int(self.cfg.get("inference_timeout", 0) or 0)
        except Exception:
            timeout = 0
        add("推理超时足够", timeout >= 300, f"{timeout}s（建议 ≥600）", "warning")
        add("任务记录目录可读", _task_log_dir(self.plugin).is_dir(), str(_task_log_dir(self.plugin)), "warning")
        add("成品目录可读", _out_dir(self.plugin).is_dir(), str(_out_dir(self.plugin)), "warning")

        errors = self._recent_errors(limit=40)
        return _ok({
            "health": health,
            "checks": checks,
            "errors": errors,
            "gateway_models": info["models"],
            "paths": {
                "gateway_work": str(_gateway_work(self.plugin) or "(未配置)"),
                "upload": str(_upload_dir(self.plugin)),
                "output": str(_out_dir(self.plugin)),
                "task_logs": str(_task_log_dir(self.plugin)),
                "plugin_data": str(_data_dir()),
            },
            "version": {"plugin": self._plugin_version()},
        })

    def _recent_errors(self, limit: int = 40) -> list[dict]:
        """从 AstrBot 日志里截取本插件相关的错误行（只读、有上限）。"""
        if not _log_file().is_file():
            return []
        rows: list[dict] = []
        try:
            with open(_log_file(), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 400 * 1024))
                tail = f.read().decode("utf-8", errors="ignore")
        except Exception:
            return []
        lines = tail.splitlines()
        for i, line in enumerate(lines):
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
            if PLUGIN_NAME not in clean:
                continue
            if not re.search(r"\[ERRO\]|\[WARN\]|Traceback|异常", clean):
                continue
            ctx = ""
            if i + 1 < len(lines):
                nxt = re.sub(r"\x1b\[[0-9;]*m", "", lines[i + 1]).strip()
                if nxt and PLUGIN_NAME not in nxt:
                    ctx = nxt[:160]
            ts = ""
            m = re.match(r"\[(\d\d:\d\d:\d\d)", clean)
            if m:
                ts = m.group(1)
            rows.append({"text": clean.strip()[:400], "context": ctx, "time": ts})
        return rows[-limit:][::-1]
