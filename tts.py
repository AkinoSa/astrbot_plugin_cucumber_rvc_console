"""TTS 音色转换（CUSTOM-7）：AstrBot TTS → RVC 变声 → 可选混响。

数据流：
    用户文本 → AstrBot TTS Provider（get_audio，返回本地音频路径）
    → 写入网关共享目录 gateway_work/tts/<task_id>/in.<ext>
    → gradio_client 调网关 /tts_convert（独立 TTS 模型与参数组）
    → 校验成品（存在/非空/ffprobe 可解码）→ 返回 (原始音频, 转换后音频)

边界约定：
    * 本模块只复用 AstrBot 已配置的 TTS 服务商，不重复实现任何 TTS API；
    * 与歌曲翻唱共用一个网关与全局串行队列，但模型/参数完全独立（配置层隔离）；
    * 任何失败抛 TTSError，由调用方（main.py 钩子 / WebUI 路由）决定降级方式，
      绝不让 TTS 失败影响聊天主流程。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from typing import Any, Optional

from astrbot.api import logger
from astrbot.core.provider.entities import ProviderType

# 网关工作目录（宿主机路径）由插件配置 `gateway_work_dir` 提供（CUSTOM-9 通用化：
# 不再把开发者机器的路径写死为默认值）。网关把整个工作目录挂载到容器 /work，
# TTS 输入放 <gateway_work_dir>/tts/<task_id>/ 子目录。
TTS_TASK_TTL = 3600  # 与网关 GW_TTS_TTL 保持一致（秒）


def _gateway_work(plugin) -> str:
    """读插件配置里的网关工作目录（宿主机路径）；未配置返回空串。"""
    try:
        gw = str(plugin.config.get("gateway_work_dir", "") or "").strip()
    except Exception:
        gw = ""
    return gw


def _tts_task_root(plugin) -> str:
    gw = _gateway_work(plugin)
    return os.path.join(gw, "tts") if gw else ""

# TTS 转换可接受的原始音频扩展名（AstrBot TTS 产物通常是 mp3/wav）
_TTS_INPUT_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}


class TTSError(RuntimeError):
    """TTS 链路中可预期的失败（文案面向日志，不直接发群）。"""


class CooldownGate:
    """按用户 / 按群的双层冷却（CUSTOM-7 方案 8.4）。"""

    def __init__(self, user_seconds: int, group_seconds: int, max_entries: int = 2048):
        self.user_seconds = max(0, int(user_seconds or 0))
        self.group_seconds = max(0, int(group_seconds or 0))
        self._user: dict[str, float] = {}
        self._group: dict[str, float] = {}
        self._max_entries = max(64, int(max_entries))

    def check(self, user_id: str, group_id: str) -> tuple[bool, str]:
        """返回 (是否放行, 拒绝原因)。只读检查，不记录时间戳。"""
        now = time.time()
        if self.user_seconds > 0:
            last = self._user.get(str(user_id), 0.0)
            remain = self.user_seconds - (now - last)
            if remain > 0:
                return False, f"用户冷却中，还剩 {remain:.0f}s"
        gid = str(group_id or "")
        if gid and self.group_seconds > 0:
            last = self._group.get(gid, 0.0)
            remain = self.group_seconds - (now - last)
            if remain > 0:
                return False, f"群冷却中，还剩 {remain:.0f}s"
        return True, ""

    def mark(self, user_id: str, group_id: str) -> None:
        now = time.time()
        self._user[str(user_id)] = now
        gid = str(group_id or "")
        if gid:
            self._group[gid] = now
        # 防止长期运行内存膨胀：超限时按最旧丢弃
        for store in (self._user, self._group):
            if len(store) > self._max_entries:
                for key, _ in sorted(store.items(), key=lambda kv: kv[1])[: len(store) - self._max_entries]:
                    store.pop(key, None)


class TTSManager:
    """TTS 取 provider → 生成 → RVC 转换的完整链路。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    # ---------------- Provider ----------------

    async def resolve_provider(self, event: Any = None, provider_id: str = ""):
        """按 单次指定 ID → 配置指定 ID → 当前会话使用中 → 第一个 TTS 实例 找 Provider。

        注意：AstrBot 4.29 的 get_provider_by_id 是 async 方法，必须 await
        （参考插件 astrbot_plugin_rvc 在这一点上是同步调用，拿到的是 coroutine）。
        """
        pm = getattr(self.plugin.context, "provider_manager", None)
        if pm is None:
            raise TTSError("AstrBot provider_manager 不可用")

        provider_id = str(
            provider_id
            or self.plugin.config.get("tts_provider_name", "")
            or getattr(self.plugin, "tts_provider_name", "")
            or ""
        ).strip()
        if provider_id:
            try:
                provider = await pm.get_provider_by_id(provider_id)
            except Exception as exc:
                provider = None
                logger.warning(f"[TTS] 按 ID 查找 Provider {provider_id} 失败: {exc}")
            if provider and hasattr(provider, "get_audio"):
                return provider
            logger.warning(f"[TTS] 未找到 ID 为 {provider_id} 的 TTS Provider，改用当前会话默认")

        try:
            umo = getattr(event, "unified_msg_origin", None) if event is not None else None
            provider = pm.get_using_provider(ProviderType.TEXT_TO_SPEECH, umo=umo)
            if provider and hasattr(provider, "get_audio"):
                return provider
        except Exception as exc:
            logger.debug(f"[TTS] get_using_provider 失败: {exc}")

        insts = getattr(pm, "tts_provider_insts", None) or []
        if insts:
            logger.warning("[TTS] 未显式配置 TTS 服务商，使用第 1 个已配置实例（建议在 WebUI 里选定）")
            return insts[0]
        raise TTSError("没有可用的 TTS 服务商：请先在 AstrBot 服务商配置里添加并启用一个 TTS 提供商")

    # ---------------- 生成与转换 ----------------

    async def generate_raw(self, provider, text: str) -> str:
        """调用 TTS Provider 生成原始音频，返回本地路径（带有效性检查）。

        Edge TTS 等免费接口偶发 403/网络抖动，这里自动重试一次再放弃。
        """
        raw = ""
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                raw = await provider.get_audio(text)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(f"[TTS] 第 {attempt} 次 TTS 生成失败: {exc}")
                if attempt == 1:
                    await asyncio.sleep(2)
        if last_exc is not None:
            # Provider 进程内调用被 Edge TTS 风控拒绝（403）时，
            # 用独立子进程走一遍 CLI 路径——实测同机同窗 CLI 稳定成功。
            logger.warning("[TTS] Provider 连续失败，尝试 Edge TTS 子进程兜底")
            try:
                return await self._edge_subprocess_fallback(text)
            except Exception as fallback_exc:
                raise TTSError(
                    f"TTS 服务商连续 2 次生成失败: {last_exc}；"
                    f"Edge 子进程兜底也失败: {fallback_exc}"
                ) from last_exc
        if not raw or not os.path.isfile(str(raw)):
            raise TTSError(f"TTS Provider 未返回有效音频路径: {raw!r}")
        if os.path.getsize(str(raw)) == 0:
            raise TTSError("TTS 生成的音频文件为空")
        ext = os.path.splitext(str(raw))[1].lower()
        if ext and ext not in _TTS_INPUT_EXTS:
            logger.warning(f"[TTS] TTS 产物扩展名 {ext} 不在常见列表里，仍尝试交给网关转换")
        return str(raw)

    def _edge_voice(self) -> str:
        """从 AstrBot 全局配置里读 edge_tts 服务商配置的音色；读不到用默认。"""
        try:
            cfg = self.plugin.context.get_config()
            for p in (cfg.get("provider") or []):
                if str(p.get("type") or "") == "edge_tts":
                    voice = str(p.get("edge-tts-voice") or "").strip()
                    if voice:
                        return voice
        except Exception:
            pass
        return "zh-CN-XiaoxiaoNeural"

    async def _edge_subprocess_fallback(self, text: str) -> str:
        """独立子进程调用 edge-tts CLI 生成语音（与 Provider 同音色）。

        为什么用子进程：实测同一台机器、同一时间窗，命令行方式对
        speech.platform.bing.com 稳定成功（8/8），而 AstrBot 进程内调用被
        服务端风控 403。子进程完全复刻成功路径，进程内环境差异不再影响结果。
        """
        import sys

        out_dir = os.path.join(_tts_task_root(self.plugin), "_direct")
        if not _gateway_work(self.plugin):
            raise TTSError("未配置 gateway_work_dir（网关工作目录），无法落盘 TTS 音频")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"edge_{uuid.uuid4().hex[:12]}.mp3")
        cmd = [
            sys.executable, "-m", "edge_tts",
            "--text", text,
            "--voice", self._edge_voice(),
            "--write-media", out,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError as exc:
            raise TTSError("edge-tts 子进程超时（90s）") from exc
        if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
            err = (stderr or b"").decode("utf-8", "replace")[-200:]
            raise TTSError(f"edge-tts 子进程失败（rc={proc.returncode}）: {err}")
        return out

    def _build_kwargs(self, client, src_host_path: str,
                      model_override: str = "", reverb_override: Optional[bool] = None,
                      pitch_override: Optional[int] = None,
                      format_override: str = "") -> dict:
        """组 /tts_convert 参数；按名字探测，网关不支持 TTS 端点时直接报错。"""
        names = self.plugin._client_api_parameter_names(client, "/tts_convert")
        if not names:
            raise TTSError("网关不支持 /tts_convert 接口：请升级 rvc-gateway 后重试")
        cfg = self.plugin.config
        # 全部实时读配置：WebUI 改模型/参数即生效，无需重载插件
        model = str(model_override or cfg.get("tts_rvc_model", "")
                    or getattr(self.plugin, "tts_rvc_model", "") or "")
        pitch = int(pitch_override) if pitch_override is not None else int(cfg.get("tts_pitch", 0) or 0)
        fmt = str(format_override or cfg.get("tts_output_format", "wav") or "wav")
        # 注意：Gradio 端点的参数名取自 /tts_convert 函数签名的形参名
        # （tts_src / model_dropdown / key_shift / ...），不是前端组件的 label。
        values = {
            "tts_src": src_host_path,
            "model_dropdown": model,
            "key_shift": pitch,
            "f0_method": str(cfg.get("tts_f0_method", "rmvpe") or "rmvpe"),
            # 网关该参数是 Textbox：必须按字符串传（CUSTOM-6.8 教训）
            "index_rate": str(cfg.get("tts_index_rate", 0.75)),
            "protect": float(cfg.get("tts_protect", 0.33) or 0.33),
            "rms_mix_rate": float(cfg.get("tts_rms_mix_rate", 0.35) or 0.35),
            "resample_sr": int(cfg.get("tts_resample_sr", 0) or 0),
            "output_format": fmt,
        }
        missing = [k for k in values if k not in names]
        if missing:
            raise TTSError(f"网关 /tts_convert 缺少参数 {missing}，请升级 rvc-gateway")
        # 混响组：TTS 独立配置，网关支持才传；reverb_override 供控制台单次开关
        reverb_on = (bool(cfg.get("tts_reverb_enabled", False))
                     if reverb_override is None else bool(reverb_override))
        rev = {
            "reverb_enabled": reverb_on,
            "reverb_wet": float(cfg.get("tts_reverb_wet", 0.18) or 0.18),
            "reverb_dry": float(cfg.get("tts_reverb_dry", 1.0) or 1.0),
            "reverb_decay": float(cfg.get("tts_reverb_decay", 1.4) or 1.4),
            "reverb_room_size": float(cfg.get("tts_reverb_room_size", 0.65) or 0.65),
            "reverb_damping": float(cfg.get("tts_reverb_damping", 0.45) or 0.45),
            "reverb_pre_delay": float(cfg.get("tts_reverb_pre_delay", 18.0) or 18.0),
            "reverb_width": float(cfg.get("tts_reverb_width", 0.8) or 0.8),
        }
        values.update({k: v for k, v in rev.items() if k in names})
        return values

    async def convert(self, text: str, event: Any = None, provider_id: str = "",
                      model: str = "", reverb_override: Optional[bool] = None,
                      pitch_override: Optional[int] = None,
                      format_override: str = "") -> tuple[str, str, dict]:
        """完整链路：TTS 生成 → 网关 RVC 转换。返回 (原始音频, 转换后音频, 网关结果)。

        provider_id / model / reverb_override / pitch_override / format_override
        供 WebUI 控制台对单次请求做覆盖，不传时全部走插件配置。
        """
        provider = await self.resolve_provider(event, provider_id=provider_id)
        raw_path = await self.generate_raw(provider, text)

        # 写入网关共享目录（宿主机 AstrBot 直写，网关容器内同目录可读）
        task_id = uuid.uuid4().hex[:12]
        task_dir = os.path.join(_tts_task_root(self.plugin), task_id)
        os.makedirs(task_dir, exist_ok=True)
        ext = os.path.splitext(raw_path)[1].lower() or ".wav"
        in_path = os.path.join(task_dir, f"in{ext}")
        try:
            shutil.copy2(raw_path, in_path)
        except OSError as exc:
            raise TTSError(f"无法写入网关共享目录 {task_dir}: {exc}") from exc

        try:
            base_url = str(self.plugin.config.get("rvc_base_url", "") or "").strip() or self.plugin.rvc_base_url
        except Exception:  # noqa: BLE001
            base_url = self.plugin.rvc_base_url
        timeout = max(60, int(getattr(self.plugin, "tts_timeout", 300) or 300))
        try:
            async with self.plugin._get_gradio_client(base_url) as client:
                kwargs = self._build_kwargs(client, in_path, model_override=model,
                                            reverb_override=reverb_override,
                                            pitch_override=pitch_override,
                                            format_override=format_override)
                result = await self.plugin._async_predict(
                    client, api_name="/tts_convert", timeout=timeout, event=event,
                    **kwargs,
                )
        except TTSError:
            raise
        except Exception as exc:
            raise TTSError(f"网关 TTS 转换失败: {exc}") from exc

        host_output = ""
        info: dict = {}
        if isinstance(result, dict):
            info = result
            host_output = str(result.get("host_output") or "")
        if not host_output:
            raise TTSError(f"网关返回结果里没有成品路径: {result!r}")
        if not os.path.isfile(host_output) or os.path.getsize(host_output) == 0:
            raise TTSError(f"成品文件不存在或为空: {host_output}")
        if not self._probe_ok(host_output):
            raise TTSError(f"成品无法解码（ffprobe 校验失败）: {host_output}")
        logger.info(f"[TTS] 转换完成: {host_output}（原始 {raw_path}）")
        return raw_path, host_output, info

    @staticmethod
    def _probe_ok(path: str) -> bool:
        """ffprobe 校验成品可解码；本机没有 ffprobe 时按文件非空放行。"""
        import subprocess

        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                return False
            return float(proc.stdout.strip().splitlines()[0]) > 0
        except FileNotFoundError:
            return os.path.getsize(path) > 0
        except Exception:
            return False

    # ---------------- 维护 ----------------

    @staticmethod
    def sweep_task_dirs(ttl_seconds: int = TTS_TASK_TTL) -> int:
        """清理过期 TTS 任务目录（只动 TTS_TASK_ROOT 下超 TTL 的，绝不动 out/）。"""
        removed = 0
        root = _tts_task_root(plugin)
        if not root or not os.path.isdir(root):
            return 0
        now = time.time()
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            try:
                if now - os.path.getmtime(d) > max(60, ttl_seconds):
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
        if removed:
            logger.info(f"[TTS] 清理过期任务目录 {removed} 个")
        # 兜底直连产物（_direct/*.mp3）按同样 TTL 清理
        direct_dir = os.path.join(root, "_direct")
        if os.path.isdir(direct_dir):
            for name in os.listdir(direct_dir):
                f = os.path.join(direct_dir, name)
                if not os.path.isfile(f):
                    continue
                try:
                    if now - os.path.getmtime(f) > max(60, ttl_seconds):
                        os.unlink(f)
                except OSError:
                    pass
        return removed
