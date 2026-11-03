from pathlib import Path
import os
import re
import uuid
import time

import asyncio
import shutil
import json
import sys
from datetime import datetime
from typing import Optional, Dict, List, Any, Set
import traceback
from contextlib import asynccontextmanager
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Record, File, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Image as CompImage
from astrbot.api import logger
from functools import partial
from gradio_client import Client

from . import netease_fetch
from . import source_router
from .selection import CoverSelectionUI
from .outbound import Outbound

#: 插件名（AstrBot 的数据目录按这个名字分：data/plugin_data/<plugin_name>）
PLUGIN_NAME = "astrbot_plugin_cucumber_rvc_console"


def _plugin_data_dir() -> str:
    """插件自己的持久化目录：``<AstrBot data>/plugin_data/astrbot_plugin_cucumber_rvc_console``。

    审核要求：用户数据不能放在 ``data/`` 根目录（会跟 AstrBot 自己的文件混在一起，
    迁移和审计都分不清），也不能写在插件代码目录（升级会被覆盖）。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = get_astrbot_plugin_data_path()
    except Exception:  # 兜底：脱离 AstrBot 跑单测时用相对路径
        base = os.path.join(os.getcwd(), "data", "plugin_data")
    path = os.path.join(base, PLUGIN_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _temp_audio_dir() -> str:
    """临时音频目录：``<plugin_data>/temp_audio``（24 小时后自动清理）。"""
    path = os.path.join(_plugin_data_dir(), "temp_audio")
    os.makedirs(path, exist_ok=True)
    return path
from .intent_gate import (
    cover_refusal,
    has_cover_ticket,
    is_cover_request,
    is_music_request,
    is_tts_request,
    message_text as event_message_text,
)
from .tts import CooldownGate, TTSError, TTSManager
from .task_registry import TaskRegistry, ST_QUEUED, ST_RUNNING, ST_CANCELLING

MODEL_ALIAS_SEPARATOR = "|||"

# 0.10.1：设置页删掉了一批历史遗留 / 重复 / 空转项，代码里保留固定默认值，
# 免得删键之后 .get 取不到值时行为漂移，也避免 KeyError。
# - default_model_index：与 rvc_default_model 下拉重复（一个序号一个名字），
#   统一回落到模型列表第 1 个；用户偏好里的字段也改名为 model_index。
FALLBACK_MODEL_INDEX = 1
# - msst_default_model：当前 chorus-mw 的分离模型由后端启动配置固定，
#   这个值只作为 /切换msst模型 的运行时状态，不再从设置页读。
MSST_DEFAULT_MODEL = "3_HP-Vocal-UVR.pth"


def _bounded_float(config: Any, key: str, default: float, low: float, high: float) -> float:
    """读一个浮点配置并夹到 [low, high]。

    设置页的输入不一定可信（老配置可能存成字符串、也可能被人手改成越界值），
    统一在这里兜底，免得一个坏值让整次翻唱失败。
    """
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        logger.warning(f"{key} 配置格式错误，使用默认值 {default}")
        value = float(default)
    clamped = max(float(low), min(float(high), value))
    if clamped != value:
        logger.warning(f"{key}={value} 超出 {low}~{high}，按 {clamped} 处理")
    return clamped


class _OutboundTarget:
    """把"主动发给指定会话"包装成 outbound 能用的最小 event 形状。

    统一出口 ``Outbound.say`` 只用到三样东西：``plain_result``、``send``、
    ``unified_msg_origin``。提醒管理员时当前事件不是管理员会话，又不能绕过
    出口直接 ``event.send``，所以在这里做一个薄适配层：消息照样由出口层组织
    语言，只是最终发到 ``context.send_message`` 指定的 session。
    """

    def __init__(self, context: Any, session: str, unified_msg_origin: str = ""):
        self._context = context
        self._session = str(session or "")
        self.unified_msg_origin = unified_msg_origin

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult().message(str(text))

    async def send(self, result: Any) -> bool:
        session = self._session
        if not session:
            return False
        chain = result if isinstance(result, MessageChain) else MessageChain().message(str(result))
        try:
            sent = await self._context.send_message(session, chain)
        except Exception as exc:
            logger.warning(f"主动发送到会话 {session} 失败: {exc}")
            return False
        return sent is not False


# CUSTOM-9 通用化：网关成品目录改为配置 `gateway_work_dir`（宿主机路径）派生，
# 不再把开发者机器的路径写死为默认值；未配置时补发/历史回放功能停用并提示。

# ==================== CUSTOM-4：配置分组适配层 ====================
# Schema 改成「外层 object 卡片 + 内层 object 子卡片」后，配置 JSON 的字段路径变成嵌套的；
# 插件里几十处 config.get("扁平键") 保持不变，由这一层把扁平键映射到嵌套路径，
# 读不到嵌套值时回退旧扁平键（同一进程内兼容旧配置）。
CONFIG_PATHS = {
    "enable_rvc": ("basic", "enable_rvc"),
    "chorus_seconds": ("basic", "chorus_seconds"),
    "render_mode": ("basic", "render_mode"),
    "inference_timeout": ("basic", "inference_timeout"),
    "default_key_shift": ("basic", "default_key_shift"),
    "auto_pick_song": ("selection", "auto_pick_song"),
    "auto_pick_fallback_menu": ("selection", "auto_pick_fallback_menu"),
    "auto_pick_model": ("selection", "auto_pick_model"),
    "auto_pick_min_score": ("selection", "auto_pick_min_score"),
    "auto_pick_min_margin": ("selection", "auto_pick_min_margin"),
    "source_order": ("selection", "source_order"),
    "timeout": ("selection", "timeout"),
    "enable_smart_cover": ("selection", "smart", "enable_smart_cover"),
    "enable_enhanced_context": ("selection", "smart", "enable_enhanced_context"),
    "enable_confirm_mechanism": ("selection", "smart", "enable_confirm_mechanism"),
    "enable_batch_cover": ("selection", "smart", "enable_batch_cover"),
    "enable_preference_learning": ("selection", "smart", "enable_preference_learning"),
    "llm_force_mode": ("selection", "smart", "llm_force_mode"),
    "enable_inject_context": ("selection", "smart", "enable_inject_context"),
    "enable_llm_success_notify": ("selection", "smart", "enable_llm_success_notify"),
    "notify_progress": ("selection", "smart", "notify_progress"),
    "notify_retry": ("selection", "smart", "notify_retry"),
    "enable_config_report": ("selection", "smart", "enable_config_report"),
    "rvc_base_url": ("rvc", "rvc_base_url"),
    "rvc_default_model": ("rvc", "models", "rvc_default_model"),
    "rvc_models_keywords": ("rvc", "models", "rvc_models_keywords"),
    "rvc_models_dir": ("rvc", "models", "rvc_models_dir"),
    "f0_method": ("rvc", "inference", "f0_method"),
    "index_rate": ("rvc", "inference", "index_rate"),
    "rms_mix_rate": ("rvc", "inference", "rms_mix_rate"),
    "protect": ("rvc", "inference", "protect"),
    "max_batch_size": ("rvc", "inference", "max_batch_size"),
    "enable_auto_key_shift": ("tuning", "shift", "enable_auto_key_shift"),
    "gender_detection_timeout": ("tuning", "shift", "gender_detection_timeout"),
    "male_to_female_shift": ("tuning", "shift", "male_to_female_shift"),
    "female_to_male_shift": ("tuning", "shift", "female_to_male_shift"),
    "enable_auto_key_shift_debug": ("tuning", "shift", "enable_auto_key_shift_debug"),
    "model_genders": ("tuning", "gender", "model_genders"),
    "model_genders_extra": ("tuning", "gender", "model_genders_extra"),
    "artist_gender_map": ("tuning", "gender", "artist_gender_map"),
    "model_gender_map": ("tuning", "gender", "model_gender_map"),
    "vocal_volume_db": ("separation", "vocal_volume_db"),
    "inst_volume_db": ("separation", "inst_volume_db"),
    "shift_accompaniment": ("separation", "shift_accompaniment"),
    "enable_send_file": ("separation", "output", "enable_send_file"),
    "enable_fade": ("separation", "output", "enable_fade"),
    "enable_local_audio_cover": ("local_audio", "enable_local_audio_cover"),
    "max_local_audio_size_mb": ("local_audio", "max_local_audio_size_mb"),
    "local_audio_cache_ttl": ("local_audio", "local_audio_cache_ttl"),
    "local_audio_trigger_keywords": ("local_audio", "local_audio_trigger_keywords"),
    "local_audio_auto_trigger": ("local_audio", "local_audio_auto_trigger"),
    "local_audio_default_model": ("local_audio", "local_audio_default_model"),
    "local_audio_default_shift": ("local_audio", "local_audio_default_shift"),
    "netease_quality": ("sources", "netease_quality"),
    "music_api_timeout": ("sources", "music_api_timeout"),
    "netease_cookie": ("sources", "netease_cookie"),
    "netease_meting_base": ("sources", "netease_meting_base"),
    "netease_api_base": ("sources", "netease_api_base"),
    "netease_api_token": ("sources", "netease_api_token"),
    "alert_admin_on_cookie_expired": ("sources", "alert_admin_on_cookie_expired"),
    "cookie_alert_interval_hours": ("sources", "cookie_alert_interval_hours"),
    "cover_queue_limit": ("advanced", "cover_queue_limit"),
    "task_queue_limit": ("advanced", "task_queue_limit"),
    "duplicate_window_seconds": ("advanced", "duplicate_window_seconds"),
    "queue_position_notice": ("advanced", "queue_position_notice"),
    "notify_persona_id": ("tuning", "notify_persona_id"),
    "reverb_enabled": ("separation", "reverb_enabled"),
    "reverb_wet": ("separation", "reverb_wet"),
    "reverb_dry": ("separation", "reverb_dry"),
    "reverb_decay": ("separation", "reverb_decay"),
    # CUSTOM-7：卷积混响扩展参数（与既有 reverb_* 同在 separation 卡片，旧值不动）
    "reverb_room_size": ("separation", "reverb_room_size"),
    "reverb_damping": ("separation", "reverb_damping"),
    "reverb_pre_delay": ("separation", "reverb_pre_delay"),
    "reverb_width": ("separation", "reverb_width"),
    # CUSTOM-7：legacy aecho 回声（默认关，高级可选）
    "echo_enabled": ("separation", "echo", "echo_enabled"),
    "echo_wet": ("separation", "echo", "echo_wet"),
    "echo_decay": ("separation", "echo", "echo_decay"),
    # CUSTOM-7：TTS 语音回复（模型/参数/触发与歌曲翻唱完全独立）
    "tts_enable": ("tts", "tts_enable"),
    "tts_provider_name": ("tts", "tts_provider_name"),
    "tts_rvc_model": ("tts", "model", "tts_rvc_model"),
    "tts_pitch": ("tts", "inference", "tts_pitch"),
    "tts_f0_method": ("tts", "inference", "tts_f0_method"),
    "tts_index_rate": ("tts", "inference", "tts_index_rate"),
    "tts_protect": ("tts", "inference", "tts_protect"),
    "tts_rms_mix_rate": ("tts", "inference", "tts_rms_mix_rate"),
    "tts_resample_sr": ("tts", "inference", "tts_resample_sr"),
    "tts_output_format": ("tts", "inference", "tts_output_format"),
    "tts_timeout": ("tts", "inference", "tts_timeout"),
    "tts_group_trigger": ("tts", "trigger", "tts_group_trigger"),
    "tts_group_require_at": ("tts", "trigger", "tts_group_require_at"),
    "tts_allow_refuse": ("tts", "trigger", "tts_allow_refuse"),
    "tts_repeat_delay": ("tts", "trigger", "tts_repeat_delay"),
    "tts_queue_limit": ("tts", "trigger", "tts_queue_limit"),
    "gateway_work_dir": ("advanced", "gateway_work_dir"),
    "tts_private_trigger": ("tts", "trigger", "tts_private_trigger"),
    "tts_user_cooldown": ("tts", "trigger", "tts_user_cooldown"),
    "tts_group_cooldown": ("tts", "trigger", "tts_group_cooldown"),
    "tts_max_text_chars": ("tts", "trigger", "tts_max_text_chars"),
    "tts_degrade_on_fail": ("tts", "trigger", "tts_degrade_on_fail"),
    "tts_reverb_enabled": ("tts", "reverb", "tts_reverb_enabled"),
    "tts_reverb_wet": ("tts", "reverb", "tts_reverb_wet"),
    "tts_reverb_dry": ("tts", "reverb", "tts_reverb_dry"),
    "tts_reverb_decay": ("tts", "reverb", "tts_reverb_decay"),
    "tts_reverb_room_size": ("tts", "reverb", "tts_reverb_room_size"),
    "tts_reverb_damping": ("tts", "reverb", "tts_reverb_damping"),
    "tts_reverb_pre_delay": ("tts", "reverb", "tts_reverb_pre_delay"),
    "tts_reverb_width": ("tts", "reverb", "tts_reverb_width"),
    "task_timeout_seconds": ("advanced", "task_timeout_seconds"),
    "enable_task_tracking": ("advanced", "enable_task_tracking"),
    "preference_storage_path": ("advanced", "preference_storage_path"),
    "qq_official_cards": ("advanced", "qq_official_cards"),
    "qq_official_buttons": ("advanced", "qq_official_buttons"),
}

# 与 _conf_schema.json 里 model_genders.items 保持一致的模型键（其余模型写 model_genders_extra）
SCHEMA_GENDER_MODEL_KEYS = ("deng", "nuoyemu", "rym")

_MISSING = object()


# 提取结果里的拒绝话术（用于「允许拒绝=关」时的兜底判定）
_TTS_REFUSE_RE = re.compile(r"不说|不会说|不能说|不想说|说不出|拒绝|别再问|做不到|才不要")

# 用户消息里的「要求语」前缀（直取兜底用）
_TTS_REQUIRE_PREFIX_RE = re.compile(
    r"^(?:我想听你说|我想让你说|想听你说|听你说|我想听你讲|你说说|给我说说|给我说|"
    r"说一句|说一説|来一句|念一句|读一句|说一说|说说看|说说|念一下|读一下|"
    r"用声音说|语音说|说个|讲个|说|讲)"
)


class _NestedConfig:
    """扁平键名 <-> 嵌套路径的读写适配层；save_config 直接代理给 AstrBotConfig。"""

    def __init__(self, raw, path_map=None):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_map", path_map or CONFIG_PATHS)

    def _node_get(self, path):
        node = self._raw
        for seg in path:
            if not isinstance(node, dict) or seg not in node:
                return _MISSING
            node = node[seg]
        return node

    def _node_set(self, path, value):
        node = self._raw
        for seg in path[:-1]:
            child = node.get(seg)
            if not isinstance(child, dict):
                child = {}
                node[seg] = child
            node = child
        node[path[-1]] = value

    def _resolve(self, key):
        if "." in key:
            # page_api 传的是 "basic.chorus_seconds" 这类点号键，直接按路径取
            value = self._node_get(tuple(key.split(".")))
            if value is not _MISSING:
                return value
        path = self._map.get(key)
        if path:
            value = self._node_get(path)
            if value is not _MISSING and value is not None:
                return value
        if isinstance(self._raw, dict):
            return self._raw.get(key, _MISSING)
        return _MISSING

    def get(self, key, default=None):
        value = self._resolve(key)
        return default if value is _MISSING else value

    def __getitem__(self, key):
        value = self._resolve(key)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        if "." in key:
            self._node_set(tuple(key.split(".")), value)
            return
        path = self._map.get(key)
        if path:
            self._node_set(path, value)
        else:
            self._raw[key] = value

    def __contains__(self, key):
        return self._resolve(key) is not _MISSING

    def keys(self):
        return self._raw.keys()

    def save_config(self, *args, **kwargs):
        return self._raw.save_config(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_raw"), name)


def resolve_resend_file(token: str, out_dir: str):
    """把 /补发翻唱 的参数解析成网关成品文件。

    支持：task_id 前几位（bc241ced8e31）、完整文件名、绝对路径（必须在成品目录内）。
    返回 (Path | None, 说明)。只读校验，不做任何删除/移动。
    """
    from pathlib import Path as _Path

    base = _Path(out_dir).resolve()
    if not base.is_dir():
        return None, f"成品目录不存在: {out_dir}"
    raw = str(token or "").strip().strip('"').strip("'")
    if not raw:
        return None, "没有给文件名"
    if "/" in raw or "\\" in raw:
        cand = _Path(raw).resolve()
        if not cand.is_relative_to(base):
            return None, "只允许补发成品目录里的文件"
        if not cand.is_file():
            return None, f"文件不存在: {cand}"
        return cand, "ok"
    name = raw[:-4] if raw.lower().endswith(".wav") else raw
    for suffix in (".wav", ".picked.wav", ""):
        cand = (base / f"{name}{suffix}").resolve()
        if cand.is_file():
            return cand, "ok"
    hits = sorted(base.glob(f"{name}*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if hits:
        return hits[0].resolve(), "ok"
    return None, f"没找到匹配 {raw} 的成品"


def list_recent_outputs(out_dir: str, limit: int = 6):
    """列出成品目录里最近的若干成品（按修改时间倒序）。"""
    from pathlib import Path as _Path

    base = _Path(out_dir)
    if not base.is_dir():
        return []
    files = [p for p in base.glob("*.wav") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:max(1, limit)]


# CUSTOM-9 更名迁移：旧插件名。仅当检测到旧版数据时使用（迁移路径），不作为默认值。
LEGACY_PLUGIN_NAME = "astrbot_plugin_chorus_cover"
# 遗留部署请手动填写 gateway_work_dir(不预填开发者路径)
_LEGACY_GATEWAY_WORK = ""


def _migrate_legacy_name_data(config) -> None:
    """把旧插件名的配置值与 plugin_data 内容迁到新名下（幂等，只补缺）。

    处理三件事：
      1. data/config/astrbot_plugin_chorus_cover_config.json 的配置值 → 当前配置
         （仅补缺：新配置里已有值/用户已填写的一律不动）；
      2. data/plugin_data/astrbot_plugin_chorus_cover/ 下的历史与偏好文件 → 新目录
         （移动，旧目录内容保留，失败不影响启动）；
      3. 从旧版迁移的部署，若 gateway_work_dir 未配置，写入旧部署的实际路径
         （这条只在检测到旧插件配置时发生，通用新装版不会出现）。
    """
    import json as _json

    data_root = ""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        data_root = get_astrbot_data_path()
    except Exception:  # noqa: BLE001
        data_root = os.path.join(os.getcwd(), "data")
    legacy_config_path = os.path.join(data_root, "config", f"{LEGACY_PLUGIN_NAME}_config.json")
    legacy_detected = os.path.isfile(legacy_config_path)

    if legacy_detected:
        try:
            with open(legacy_config_path, encoding="utf-8-sig") as f:
                legacy = _json.load(f)
        except Exception as exc:  # noqa: BLE001
            legacy = {}
            logger.warning(f"[RVC 控制台] 读取旧配置失败: {exc}")
        if isinstance(legacy, dict):
            changed = False

            def _set(path, value):
                """按嵌套路径写值（迁移场景：legacy 值 = 用户真实状态，直接覆盖）"""
                nonlocal changed
                node = config
                for seg in path[:-1]:
                    child = node.get(seg)
                    if not isinstance(child, dict):
                        child = {}
                        node[seg] = child
                    node = child
                if node.get(path[-1]) != value:
                    node[path[-1]] = value
                    changed = True

            for flat, path in CONFIG_PATHS.items():
                node = legacy
                ok = True
                for seg in path:
                    if not isinstance(node, dict) or seg not in node:
                        ok = False
                        break
                    node = node[seg]
                if not ok:
                    continue
                if node in (None, "", [], {}):
                    continue
                _set(path, node)

            # gateway_work_dir：legacy 配置没有这个键 → 写入旧部署的实际路径；
            # 并清理此前误写到顶层的同名键（挪到 advanced 下）
            adv = config.get("advanced")
            if not isinstance(adv, dict):
                adv = {}
                config["advanced"] = adv
            top_gw = config.pop("gateway_work_dir", None)
            if not str(adv.get("gateway_work_dir", "") or "").strip():
                adv["gateway_work_dir"] = top_gw or _LEGACY_GATEWAY_WORK
                changed = True
            if changed:
                config.save_config()
                logger.info("[RVC 控制台] 已从旧插件名迁移配置值（覆盖式，旧值即用户状态）")
        # 迁移完成后把旧配置文件改名标记，保证迁移只执行一次——
        # 否则每次重载都会用旧值覆盖用户新改的设置（实测踩坑：默认音色被改回 nuoyemu）
        try:
            import os as _os
            _os.replace(legacy_config_path, legacy_config_path + ".migrated-to-cucumber-rvc-console")
            logger.info("[RVC 控制台] 旧配置文件已标记为已迁移")
        except OSError:
            pass

    # plugin_data 文件迁移
    try:
        new_dir = _plugin_data_dir()
        legacy_dir = os.path.join(os.path.dirname(new_dir.rstrip("/")), LEGACY_PLUGIN_NAME)
        if os.path.isdir(legacy_dir):
            moved = 0
            for fn in ("user_preferences.json", "tts_history.json", "cover_history.json",
                       "webui_jobs.json"):
                src = os.path.join(legacy_dir, fn)
                dst = os.path.join(new_dir, fn)
                if os.path.isfile(src) and not os.path.isfile(dst):
                    shutil.copy2(src, dst)
                    moved += 1
            logger.info(f"[RVC 控制台] 旧插件数据迁移完成（{moved} 个文件，旧目录保留）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[RVC 控制台] 旧数据迁移失败(不影响启动): {exc}")


def _migrate_config_layout(raw) -> bool:
    """把旧扁平键的值补进嵌套路径（嵌套已存在则不动），可重复执行。"""
    changed = False
    for flat, path in CONFIG_PATHS.items():
        if flat not in raw:
            continue
        node = raw
        touched = False
        for seg in path[:-1]:
            if not isinstance(node.get(seg), dict):
                node[seg] = {}
                touched = True
            node = node[seg]
        if path[-1] not in node or node[path[-1]] is None:
            node[path[-1]] = raw[flat]
            touched = True
        changed |= touched
    return changed


@register(
    "astrbot_plugin_cucumber_rvc_console",
    "AkinoSa",
    "RVC 控制台：翻唱（取歌/分离/变声/混响）与 TTS 语音回复统一调度；对话出口统一走模型、失败结果结构化",
    "1.0.0",
    "https://github.com/AkinoSa/astrbot_plugin_cucumber_rvc_console",
)
class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # CUSTOM-9 更名迁移：旧插件名（astrbot_plugin_chorus_cover）的配置值与
        # plugin_data 内容迁到新名下（幂等，只补缺，绝不覆盖已填写值）。
        try:
            _migrate_legacy_name_data(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[RVC 控制台] 旧名数据迁移失败（不影响启动）: {exc}")
        # CUSTOM-4：配置改为分组（嵌套）结构。先把旧扁平值补进嵌套路径（幂等），
        # 再用适配层包装，后面所有 config.get("扁平键") 无需改动。
        try:
            if _migrate_config_layout(config):
                config.save_config()
                logger.info("[配置迁移] 扁平配置已补进分组结构（嵌套优先，仅补缺）")
        except Exception as exc:  # noqa: BLE001
            logger.error("[配置迁移] 失败，继续按扁平键读取: %s", exc)
        config = _NestedConfig(config)
        self.config = config
        # 统一对话出口：进度/重试默认静音，失败只说一句话（台词由人格模型现编）
        self.outbound = Outbound(self)
        
        self.rvc_base_url = config.get("rvc_base_url", "http://127.0.0.1:3333/")
        # 搜歌/取歌用哪个平台由 source_order 决定（默认网易云 → 酷狗 → 酷我 → QQ 音乐）
        self.timeout = config.get("timeout", 60)
        self.selection_ui = CoverSelectionUI(
            buttons=bool(config.get("qq_official_buttons", True)),
            config_getter=context.get_config,
            cards=bool(config.get("qq_official_cards", True)),
        )
        
        self.rvc_models_keywords = config.get("rvc_models_keywords", [])
        # 0.11.0：音色模型文件夹。留空=用中间层默认目录；填了会拿去和中间层核对
        self.rvc_models_dir = str(config.get("rvc_models_dir", "") or "").strip()
        self._models_dir_info = {}
        self.enable_rvc = config.get("enable_rvc", True)

        self.inference_timeout = config.get("inference_timeout", 300)
        self.music_api_timeout = max(5, int(config.get("music_api_timeout", 20)))

        # === 网易云取歌（0.5.0：照搬点歌插件 astrbot_plugin_music）===
        self.netease_cookie = str(config.get("netease_cookie", "") or "").strip()
        try:
            quality = int(str(config.get("netease_quality", 320) or 320).strip() or 320)
        except (TypeError, ValueError):
            logger.warning("netease_quality 配置格式错误，使用默认值 320")
            quality = 320
        if quality not in (128, 192, 320):
            logger.warning(f"netease_quality={quality} 不在 128/192/320 之内，已按 320 处理")
            quality = 320
        self.netease_quality = quality
        # 取直链的两条路都来自点歌插件的平台配置：meting（netEase 平台）+ NodeJS 服务（netease_api 平台）
        raw_meting_base = str(config.get("netease_meting_base", "") or "").strip()
        self.netease_meting_base = (
            netease_fetch.normalize_meting_base(raw_meting_base)
            if raw_meting_base
            else netease_fetch.DEFAULT_METING_BASE
        )
        raw_api_base = str(config.get("netease_api_base", "") or "").strip()
        self.netease_api_base = netease_fetch.normalize_api_base(raw_api_base)
        if raw_api_base and not self.netease_api_base:
            logger.warning(f"netease_api_base={raw_api_base} 不像是可用地址，已忽略")
        self.netease_fetcher = netease_fetch.NeteaseFetcher(
            timeout=self.music_api_timeout,
            cookie=self.netease_cookie,
            meting_base=self.netease_meting_base,
            api_base=self.netease_api_base,
            api_token=str(config.get("netease_api_token", "") or "").strip(),
        )
        # === 多音源路由（0.7.0）：按 source_order 依次尝试，前一个没成自动换下一个 ===
        # 网易云用上面这个取歌器（原单源逻辑），酷狗/酷我/QQ 音乐动态 import
        # 点歌插件 astrbot_plugin_music 的现成平台类，配置也从它那里读。
        plugin_dir = Path(__file__).resolve().parent
        self.source_order = source_router.normalize_source_order(
            config.get("source_order", list(source_router.DEFAULT_SOURCE_ORDER))
        )
        self.song_router = source_router.SourceRouter(
            source_order=self.source_order,
            netease_fetcher=self.netease_fetcher,
            dest_dir=self._netease_temp_dir(),
            music_plugin_dir=plugin_dir.parent / "astrbot_plugin_music",
            music_config_path=(
                plugin_dir.parent.parent
                / "config"
                / "astrbot_plugin_music_config.json"
            ),
            timeout=self.music_api_timeout,
            download_timeout=max(60, self.timeout),
            limit=10,
            quality_br=self._netease_quality_br(),
        )
        logger.info(
            f"[音源路由] 顺序：{source_router.describe_order(self.source_order)}"
            "（改插件配置 source_order 可调整顺序/停用某个源）"
        )
        self.alert_admin_on_cookie_expired = bool(
            config.get("alert_admin_on_cookie_expired", True)
        )
        try:
            self.cookie_alert_interval_hours = max(
                1, int(config.get("cookie_alert_interval_hours", 6))
            )
        except (TypeError, ValueError):
            logger.warning("cookie_alert_interval_hours 配置格式错误，使用默认值 6")
            self.cookie_alert_interval_hours = 6
        # 连续 cookie 相关失败计数 + 上次提醒时间（同一失效状态按间隔去重）
        self._netease_cookie_fail_streak = 0
        self._cookie_alert_sent_at = 0.0
        
        self.llm_force_mode = config.get("llm_force_mode", False)
        self.enable_smart_cover = config.get("enable_smart_cover", True)
        self.enable_enhanced_context = config.get("enable_enhanced_context", True)
        self.enable_confirm_mechanism = config.get("enable_confirm_mechanism", True)
        self.enable_batch_cover = config.get("enable_batch_cover", True)
        self.enable_preference_learning = config.get("enable_preference_learning", True)
        
        self.enable_llm_success_notify = config.get("enable_llm_success_notify", True)
        # CUSTOM-5：首尾淡入淡出（0.5 秒），失败/旧网关不支持时自动跳过并记日志
        self.enable_fade = bool(config.get("enable_fade", True))
        self.enable_send_file = config.get("enable_send_file", False)
        self.enable_config_report = config.get("enable_config_report", True)
        
        # === 方案A+B：上下文注入 + 任务追踪 ===
        self.enable_inject_context = config.get("enable_inject_context", True)
        self.enable_task_tracking = config.get("enable_task_tracking", True)
        self.task_timeout_seconds = config.get("task_timeout_seconds", 9000)
        # CUSTOM-6.6：最多允许几人排队（0 = 不排队，正在跑的不计入）
        try:
            self.cover_queue_limit = max(0, min(10, int(config.get("cover_queue_limit", 3))))
        except Exception:
            self.cover_queue_limit = 3
        self._cover_active = None
        self._cover_queue = []
        self._cover_slot = asyncio.Lock()
        self._pending_cover_meta = {}
        # CUSTOM-8：统一任务注册表（task_id 级状态机，跨翻唱/TTS）
        try:
            self.task_queue_limit = max(1, min(50, int(config.get("task_queue_limit", 10))))
        except (TypeError, ValueError):
            self.task_queue_limit = 10
        try:
            _dup_window = max(0.0, float(config.get("duplicate_window_seconds", 120)))
        except (TypeError, ValueError):
            _dup_window = 120.0
        self.tasks = TaskRegistry(max_pending=self.task_queue_limit,
                                  duplicate_window=_dup_window)
        self._last_register_reject_reason = ""
        
        # 0.10.1：默认模型序号设置项已删除（与 rvc_default_model 下拉重复），固定第 1 个兜底
        self.fallback_model_index = FALLBACK_MODEL_INDEX
        # 插件设置里下拉选的默认 RVC 音色（空串表示不指定，回落到第 1 个模型）
        self.rvc_default_model = str(config.get("rvc_default_model", "") or "").strip()

        # === 自动选歌 / 默认音色（本项目新增开关，默认开启）===
        # 关掉任何一个都会退回原来的手动菜单，不影响其它行为。
        self.auto_pick_song = bool(config.get("auto_pick_song", True))
        self.auto_pick_model = bool(config.get("auto_pick_model", True))
        self.auto_pick_min_score = float(config.get("auto_pick_min_score", 45.0))
        self.auto_pick_min_margin = float(config.get("auto_pick_min_margin", 8.0))
        self.auto_pick_fallback_menu = bool(config.get("auto_pick_fallback_menu", True))
        self.default_key_shift = config.get("default_key_shift", 0)
        self.f0_method = config.get("f0_method", "rmvpe")
        self.index_rate = _bounded_float(config, "index_rate", 0.75, 0.0, 1.0)
        # === 0.8.0 中间层真正会用的采样/混音参数 ===
        # 中间层 0.8.0 起把这些值按名字传进 RVC（index_rate/rms_mix_rate/protect/f0）
        # 和混音（inst_volume_db/vocal_volume_db）；老后端会在提交前按参数表探测、缺了就不传。
        self.rms_mix_rate = _bounded_float(config, "rms_mix_rate", 0.35, 0.0, 1.0)
        self.protect = _bounded_float(config, "protect", 0.33, 0.0, 0.5)
        self.inst_volume_db = _bounded_float(config, "inst_volume_db", -6.0, -20.0, 0.0)
        self.vocal_volume_db = _bounded_float(config, "vocal_volume_db", 0.0, -12.0, 12.0)
        self.shift_accompaniment = config.get("shift_accompaniment", True)

        # === 0.6.0 翻唱时长（只对 chorus-mw 这类支持 render_mode 的后端生效）===
        raw_render_mode = str(config.get("render_mode", "chorus") or "chorus").strip().lower()
        if raw_render_mode not in ("chorus", "full", "custom"):
            logger.warning(f"render_mode={raw_render_mode} 不认识，按 chorus（副歌片段）处理")
            raw_render_mode = "chorus"
        self.render_mode = raw_render_mode
        try:
            seconds = int(round(float(config.get("chorus_seconds", 45))))
        except (TypeError, ValueError):
            logger.warning("chorus_seconds 配置格式错误，使用默认值 45")
            seconds = 45
        self.chorus_seconds = max(20, min(180, seconds))

        
        # 0.10.1：MSST 批大小/重叠/归一化/TTA 与整组 UVR5 参数已从设置页删除。
        # 它们只是 /convert 的形参占位：chorus-mw 收到后不传给 pipeline，
        # 分离模型固定用后端 config.SEP_MODEL，改了也不影响成品。
        # 这里只留 /切换msst模型 需要的运行时状态（不再从设置页读）。
        self.msst_default_model = MSST_DEFAULT_MODEL
        
        self.max_batch_size = config.get("max_batch_size", 5)
        # 0.12.1：偏好文件默认放 data/plugin_data/<插件名>/，不再写 data/ 根目录。
        # 老配置里若还是旧默认值 "data/user_preferences.json"，自动迁到新位置。
        default_pref = os.path.join(_plugin_data_dir(), "user_preferences.json")
        configured_pref = str(config.get("preference_storage_path", "") or "").strip()
        legacy_pref = os.path.join("data", "user_preferences.json")
        if not configured_pref or configured_pref.replace("\\", "/") == legacy_pref.replace("\\", "/"):
            self.preference_storage_path = default_pref
            legacy_abs = os.path.join(os.getcwd(), "data", "user_preferences.json")
            if not os.path.exists(default_pref) and os.path.exists(legacy_abs):
                try:
                    shutil.copy2(legacy_abs, default_pref)
                    logger.info(f"用户偏好已从旧位置迁移到 {default_pref}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"迁移旧偏好文件失败（继续用新路径）: {exc}")
        else:
            self.preference_storage_path = configured_pref

        self.enable_auto_key_shift = config.get("enable_auto_key_shift", False)
        self.enable_auto_key_shift_debug = config.get("enable_auto_key_shift_debug", False)
        self.gender_detection_timeout = max(
            3, int(config.get("gender_detection_timeout", 15))
        )
        # 成功播报固定使用的人格 ID（留空=自动解析当前会话人格）
        self.notify_persona_id = str(config.get("notify_persona_id", "") or "").strip()
        self.male_to_female_shift = config.get("male_to_female_shift", 12)
        self.female_to_male_shift = config.get("female_to_male_shift", -12)
        self.artist_gender_map = self._parse_gender_map(config.get("artist_gender_map", []))
        # CUSTOM-4：模型性别只有一份权威数据（model_genders 下拉对象），
        # 旧 model_gender_map 列表只做一次性迁移，迁移后清空。
        self.model_gender_map = self._load_model_genders(config)
        # CUSTOM-6：AstrBot 原生插件页面（pages/dashboard）
        # 页面通过 bridge 调 /api/plug/<插件名>/page/*，这里只注册路由，不主动访问 Gateway。
        try:
            from .page_api import PageApi

            self._page_api = PageApi(self)
            self._page_api.register_routes()
            logger.info("[WebUI] 插件页面 API 已注册（%d 条路由）",
                        len(getattr(self.context, "registered_web_apis", []) or []))
        except Exception as exc:  # noqa: BLE001
            self._page_api = None
            logger.error("[WebUI] 页面 API 注册失败（聊天功能不受影响）: %s", exc)

        # === 方案E：偏好学习系统 ===
        self.user_preferences: Dict[str, Dict] = {}
        self._pref_lock = asyncio.Lock()  # 偏好数据并发锁
        self._load_preferences()
        
        # === 异步任务追踪 ===
        self._pending_tasks: Set[asyncio.Task] = set()
        
        # === 方案B：翻唱任务状态追踪 ===
        self._active_cover_tasks: Dict[str, Dict] = {}
        
        # === 本地音频缓存 ===
        self._recent_audio_cache: Dict[str, tuple[str, float]] = {}
        self._recent_audio_cache_ttl = config.get("local_audio_cache_ttl", 300)
        self.max_local_audio_size_mb = config.get("max_local_audio_size_mb", 30)
        self.enable_local_audio_cover = config.get("enable_local_audio_cover", True)
        self.local_audio_auto_trigger = config.get("local_audio_auto_trigger", False)
        # 只留「翻唱」「变声」这类明确想唱的词；「转换/处理/修音」会把只想聊天
        # 或点歌的消息也吞进来，0.4.3 起从默认词表里去掉。词表见 intent_gate.py。
        self.local_audio_trigger_keywords = config.get(
            "local_audio_trigger_keywords", ["翻唱", "变声"]
        )
        self.local_audio_default_model = config.get("local_audio_default_model", "")
        self.local_audio_default_shift = config.get("local_audio_default_shift", 0)
        self._audio_cache_lock = asyncio.Lock()

        # === CUSTOM-7：TTS 语音回复（与歌曲翻唱模型/参数完全独立） ===
        self.tts_enable = bool(config.get("tts_enable", False))
        self.tts_provider_name = str(config.get("tts_provider_name", "") or "").strip()
        self.tts_rvc_model = str(config.get("tts_rvc_model", "") or "").strip()
        self.tts_pitch = int(_bounded_float(config, "tts_pitch", 0, -24, 24))
        self.tts_f0_method = str(config.get("tts_f0_method", "rmvpe") or "rmvpe").strip()
        self.tts_index_rate = _bounded_float(config, "tts_index_rate", 0.75, 0.0, 1.0)
        self.tts_protect = _bounded_float(config, "tts_protect", 0.33, 0.0, 0.5)
        self.tts_rms_mix_rate = _bounded_float(config, "tts_rms_mix_rate", 0.35, 0.0, 1.0)
        try:
            self.tts_resample_sr = max(0, min(48000, int(config.get("tts_resample_sr", 0) or 0)))
        except (TypeError, ValueError):
            self.tts_resample_sr = 0
        fmt = str(config.get("tts_output_format", "wav") or "wav").strip().lower()
        if fmt not in ("wav", "mp3", "flac", "m4a"):
            logger.warning(f"tts_output_format={fmt} 不支持，按 wav 处理")
            fmt = "wav"
        self.tts_output_format = fmt
        try:
            self.tts_timeout = max(60, int(config.get("tts_timeout", 300) or 300))
        except (TypeError, ValueError):
            self.tts_timeout = 300
        self.tts_group_trigger = bool(config.get("tts_group_trigger", True))
        self.tts_private_trigger = bool(config.get("tts_private_trigger", True))
        self.tts_degrade_on_fail = bool(config.get("tts_degrade_on_fail", True))
        try:
            self.tts_max_text_chars = max(1, min(500, int(config.get("tts_max_text_chars", 200))))
        except (TypeError, ValueError):
            self.tts_max_text_chars = 200
        try:
            self.tts_user_cooldown = max(0, int(config.get("tts_user_cooldown", 60)))
            self.tts_group_cooldown = max(0, int(config.get("tts_group_cooldown", 30)))
        except (TypeError, ValueError):
            self.tts_user_cooldown, self.tts_group_cooldown = 60, 30
        self.tts_cooldown = CooldownGate(self.tts_user_cooldown, self.tts_group_cooldown)
        self.tts = TTSManager(self)
        # CUSTOM-7.4：TTS 队列（与翻唱队列同构，默认 3 条）
        try:
            self.tts_queue_limit = max(0, min(10, int(config.get("tts_queue_limit", 3))))
        except (TypeError, ValueError):
            self.tts_queue_limit = 3
        self._tts_active = None
        self._tts_queue = []
        self._tts_slot = asyncio.Lock()
        # CUSTOM-7：卷积混响扩展参数（歌曲翻唱用；wet/dry/decay 沿用旧键，读取即时生效）
        self.reverb_room_size = _bounded_float(config, "reverb_room_size", 0.65, 0.0, 1.0)
        self.reverb_damping = _bounded_float(config, "reverb_damping", 0.45, 0.0, 1.0)
        self.reverb_pre_delay = _bounded_float(config, "reverb_pre_delay", 18.0, 0.0, 100.0)
        self.reverb_width = _bounded_float(config, "reverb_width", 0.8, 0.0, 1.0)
        self.echo_enabled = bool(config.get("echo_enabled", False))
        self.echo_wet = _bounded_float(config, "echo_wet", 0.15, 0.0, 0.5)
        self.echo_decay = _bounded_float(config, "echo_decay", 0.4, 0.05, 0.9)
        
        # === 清理旧临时文件 ===
        self._cleanup_old_temp_files()
        # CUSTOM-7：顺手清理过期的 TTS 任务目录（网关侧也会清，双保险）
        try:
            TTSManager.sweep_task_dirs()
        except Exception as exc:
            logger.debug(f"[TTS] 启动清理任务目录失败: {exc}")
        
        # === 性别识别缓存 ===
        self._gender_cache: Dict[str, str] = {}
        self._gender_cache_max_size = 100
        

    @staticmethod
    def _onebot_response_data(response: Any) -> Dict[str, Any]:
        """Extract the data object returned by aiocqhttp/OneBot actions."""
        if not isinstance(response, dict):
            return {}
        data = response.get("data")
        return data if isinstance(data, dict) else response

    async def _find_recent_group_file(
        self,
        call_action,
        group_id: int,
        file_name: str,
        file_size: int,
    ) -> tuple[Optional[Dict[str, Any]], str]:
        """Recover an upload that succeeded before LLBot's file-card feed failed.

        LLBot uploads the binary first and then publishes a group-file feed.  QQ
        may return -1002 for the second step even though the file is already in
        the group file system.  Verify the root file list before retrying the
        whole upload so we do not create duplicates.
        """
        expected_name = str(file_name or "").strip()
        expected_size = max(0, int(file_size or 0))
        now = int(time.time())

        for check in range(3):
            if check:
                await asyncio.sleep(check)
            try:
                response = await call_action(
                    "get_group_root_files",
                    group_id=int(group_id),
                )
                data = self._onebot_response_data(response)
                files = data.get("files", [])
                if not isinstance(files, list):
                    files = []

                candidates = []
                for item in files:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("file_name", "")).strip() != expected_name:
                        continue
                    try:
                        listed_size = int(item.get("file_size", 0) or 0)
                        upload_time = int(item.get("upload_time", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if expected_size and listed_size != expected_size:
                        continue
                    # Only accept a fresh item from this upload attempt.  This
                    # avoids mistaking an old cached song with the same name.
                    if upload_time and abs(now - upload_time) > 15 * 60:
                        continue
                    candidates.append(item)

                if not candidates:
                    continue
                uploaded = max(
                    candidates,
                    key=lambda item: int(item.get("upload_time", 0) or 0),
                )
                download_url = ""
                file_id = uploaded.get("file_id")
                if file_id:
                    try:
                        url_response = await call_action(
                            "get_group_file_url",
                            group_id=int(group_id),
                            file_id=file_id,
                            busid=int(uploaded.get("busid", 0) or 0),
                        )
                        url_data = self._onebot_response_data(url_response)
                        download_url = str(url_data.get("url", "") or "").strip()
                    except Exception as url_error:
                        logger.debug(f"读取已上传群文件下载链接失败: {url_error}")
                return uploaded, download_url
            except Exception as verify_error:
                logger.debug(
                    f"核验QQ群文件上传结果失败: check={check + 1}/3 "
                    f"error={verify_error}"
                )
        return None, ""

    @staticmethod
    async def _group_pair(event: Optional[AstrMessageEvent]) -> tuple[str, str]:
        """（群号, 群名）；私聊返回 ("", "")，群名查不到时为空串。"""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            gid = ""
        if not gid:
            return "", ""
        name = ""
        call_action = getattr(getattr(event, "bot", None), "call_action", None)
        if callable(call_action):
            try:
                info = await call_action("get_group_info", group_id=int(gid))
                data = info.get("data") if isinstance(info, dict) else None
                name = str((data or {}).get("group_name") or "")
            except Exception:  # noqa: BLE001
                name = ""
        return gid, name

    @staticmethod
    def _sender_pair(event: Optional[AstrMessageEvent]) -> tuple[str, str]:
        """触发用户的 (ID, 昵称)；无事件（WebUI 控制台）时都标为 WebUI。"""
        if event is None:
            return "WebUI", "WebUI"
        try:
            uid = str(event.get_sender_id() or "").strip() or "未知"
        except Exception:
            uid = "未知"
        try:
            name = str(event.get_sender_name() or "").strip()
        except Exception:
            name = ""
        return uid, name or uid

    @staticmethod
    def _cover_history_user(event: Optional[AstrMessageEvent]) -> str:
        """触发用户的标识（QQ 号/平台 ID）；无事件（WebUI 控制台）时标为 WebUI。"""
        if event is None:
            return "WebUI"
        try:
            uid = str(event.get_sender_id() or "").strip()
            return uid or "未知"
        except Exception:
            return "未知"

    def _record_cover_history(self, result_path: str, song: str, artist: str = "",
                              model: str = "", user: str = "", user_name: str = "",
                              group_id: str = "", group_name: str = "") -> None:
        """记录成品与歌曲信息（最多 300 条），供 WebUI 任务列表显示歌名/歌手/触发用户。"""
        try:
            import json as _json

            key = os.path.basename(str(result_path)).split(".")[0]
            if not key or not song:
                return
            path = os.path.join(_plugin_data_dir(), "cover_history.json")
            data = {}
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        data = _json.load(f) or {}
                except Exception:
                    data = {}
            data[key] = {
                "song": str(song)[:80],
                "artist": str(artist or "")[:80],
                "model": str(model or "")[:60],
                "user": str(user or "")[:60],
                "user_name": str(user_name or "")[:60],
                "group_id": str(group_id or "")[:40],
                "group_name": str(group_name or "")[:60],
                "at": time.time(),
            }
            if len(data) > 300:
                kept = sorted(data.items(), key=lambda kv: (kv[1] or {}).get("at", 0),
                              reverse=True)[:300]
                data = dict(kept)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RVC控制台] 记录翻唱历史失败: %s", exc)

    async def _send_cover_result(self, event: AstrMessageEvent, result_path: str,
                                 song_name: str = "翻唱", cache_hit: bool = False,
                                 artist: str = "", model: str = "", record: bool = True):
        if not result_path or not os.path.exists(result_path):
            await event.send(event.plain_result("生成失败，后端未返回有效文件路径。"))
            return
        user_id, user_name = self._sender_pair(event)
        group_id, group_name = await self._group_pair(event)
        if record:
            self._record_cover_history(result_path, song_name, artist, model,
                                       user=user_id, user_name=user_name,
                                       group_id=group_id, group_name=group_name)
        if cache_hit and getattr(self, "enable_config_report", True):
            # 模板文案，由「成功时发送模板提示」开关统一控制
            await event.send(event.plain_result("⚡ 命中缓存，这首之前唱过，直接发给你～"))
        record_sent = False
        file_sent = False
        record_error = None
        try:
            await event.send(event.chain_result([Record(file=result_path)]))
            record_sent = True
            logger.info(f"[RVC控制台发送Debug] QQ语音发送成功: {result_path}")
        except Exception as e:
            record_error = e
            logger.error(f"QQ语音发送失败，将继续尝试发送文件: {e}")
        # 配置开启时额外发送文件；即使未开启，QQ 语音失败也必须自动兜底。
        if self.enable_send_file or not record_sent:
            file_name = os.path.basename(result_path)
            _, ext = os.path.splitext(file_name)
            if not ext:
                ext = ".mp3"
            safe_name = re.sub(r'[\\/:*?"<>|]', '', song_name).strip() or "翻唱"
            send_name = f"{safe_name}{ext}"
            file_path = os.path.abspath(result_path)

            # OneBot 群聊中的普通 File 消息可能被协议端静默忽略。优先走
            # upload_group_file，确保真正上传到群文件；其他平台/私聊再回退 File。
            group_id = event.get_group_id()
            call_action = getattr(getattr(event, "bot", None), "call_action", None)
            if group_id and callable(call_action):
                # LLBot may still be finalizing the preceding voice message. An
                # immediate file upload commonly returns code=-1002 / 请重试.
                if record_sent:
                    await asyncio.sleep(2)
                ascii_name = f"rvc_console_{int(time.time())}{ext.lower()}"
                upload_names = (send_name, send_name, ascii_name)
                upload_error = None
                for attempt, upload_name in enumerate(upload_names, start=1):
                    if attempt > 1:
                        await asyncio.sleep(2 * (attempt - 1))
                    try:
                        response = await call_action(
                            "upload_group_file",
                            group_id=int(group_id),
                            file=file_path,
                            name=upload_name,
                        )
                        file_sent = True
                        logger.info(
                            "[RVC控制台发送Debug] "
                            f"QQ群文件上传成功: name={upload_name} attempt={attempt} "
                            f"response={response!r}"
                        )
                        break
                    except Exception as e:
                        upload_error = e
                        logger.warning(
                            "[RVC控制台发送Debug] "
                            f"QQ群文件上传失败: name={upload_name} "
                            f"attempt={attempt}/{len(upload_names)} error={e}"
                        )
                        uploaded, download_url = await self._find_recent_group_file(
                            call_action,
                            int(group_id),
                            upload_name,
                            os.path.getsize(file_path),
                        )
                        if uploaded:
                            file_sent = True
                            logger.info(
                                "[RVC控制台发送Debug] "
                                "QQ群文件主体已上传，只有聊天文件卡片发布失败: "
                                f"name={upload_name} file_id={uploaded.get('file_id')}"
                            )
                            notice = (
                                f"✅ 音频已保存到群文件：{upload_name}\n"
                                "⚠️ QQ 拒绝了群聊文件卡片，已自动核验实际上传结果。"
                            )
                            if download_url:
                                notice += f"\n🔗 下载链接：{download_url}"
                            await event.send(event.plain_result(notice))
                            break
                if not file_sent:
                    logger.warning(f"QQ群文件重试全部失败，将回退普通文件消息: {upload_error}")

            if not file_sent:
                try:
                    await event.send(event.chain_result([File(name=send_name, file=file_path)]))
                    file_sent = True
                    logger.info(f"[RVC控制台发送Debug] QQ文件消息发送成功: {send_name}")
                except Exception as e:
                    logger.error(f"以文件形式发送翻唱结果失败: {e}")
        if record_sent and self.enable_send_file and not file_sent:
            await event.send(event.plain_result(
                "⚠️ 语音已发送，但 LLBot 群文件上传失败；"
                "已完成多次重试，请查看 LLBot 日志中的 upload_group_file 错误。"
            ))
        if not record_sent and not file_sent:
            raise RuntimeError(f"QQ语音和文件均发送失败: {record_error or '未知错误'}")
        if not record_sent and file_sent:
            await event.send(event.plain_result("⚠️ QQ语音上传失败，已自动改为发送音频文件。"))

    async def _send_progress_notice(
        self, event: AstrMessageEvent, step: str = "", song: str = "", **facts: Any
    ) -> None:
        """进度提示统一走 outbound 的 progress 出口。

        默认静音（缺省 notify_progress=False），要发也只发一句人话；
        出口层自己吞异常，这里不再包 try/except，也不会打断推理流程。
        """
        await self.outbound.say(event, "progress", step=step, song=song, **facts)

    def _load_preferences(self):
        """加载用户偏好数据"""
        if not self.enable_preference_learning:
            return
            
        try:
            pref_path = Path(self.preference_storage_path)
            if pref_path.exists():
                with open(pref_path, 'r', encoding='utf-8') as f:
                    self.user_preferences = json.load(f)
                logger.info(f"已加载用户偏好数据: {len(self.user_preferences)} 个用户")
        except Exception as e:
            logger.error(f"加载偏好数据失败: {e}")
            self.user_preferences = {}

    async def _save_preferences(self):
        """保存用户偏好数据（异步安全）"""
        if not self.enable_preference_learning:
            return
            
        async with self._pref_lock:
            try:
                pref_path = Path(self.preference_storage_path)
                pref_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(pref_path, 'w', encoding='utf-8') as f:
                    json.dump(self.user_preferences, f, ensure_ascii=False, indent=2)
                logger.info(f"已保存用户偏好数据: {len(self.user_preferences)} 个用户")
            except Exception as e:
                logger.error(f"保存偏好数据失败: {e}")
    
    def _cleanup_old_temp_files(self):
        """清理超过24小时的旧临时文件"""
        try:
            temp_dir = _temp_audio_dir()
            if not os.path.isdir(temp_dir):
                return
            now = time.time()
            for fname in os.listdir(temp_dir):
                fpath = os.path.join(temp_dir, fname)
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 86400:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
        except Exception as e:
            logger.debug(f"清理旧临时文件失败: {e}")

    def _is_audio_file(self, filename: Optional[str]) -> bool:
        """判断文件名是否为支持的音频格式"""
        if not filename:
            return False
        ext = os.path.splitext(filename.lower())[1]
        return ext in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".wma", ".aac", ".mp4"}

    def _reject_music_request(self, event: AstrMessageEvent, tool_name: str) -> Optional[str]:
        """翻唱入口的统一闸门：这条消息是点歌需求时不执行翻唱。

        判定逻辑和词表都在 ``intent_gate.py``，这里只负责记日志、把提示句交回模型。
        返回 ``None`` 表示放行；返回字符串表示拒绝（字符串会作为工具结果给模型）。
        """
        hint = cover_refusal(event)
        if hint:
            logger.info(
                f"[IntentGate] {tool_name} 未执行：这条消息是点歌需求 | "
                f"text={event_message_text(event)!r}"
            )
        return hint

    def _infer_key_shift_from_filename(self, filename: str, model_display: str = "") -> Optional[int]:
        """根据音频文件名自动推断合适的 key_shift（本地兜底逻辑，不依赖LLM）
        当 LLM 未传入 key_shift 时，优先使用用户配置的映射表和数值进行推断。
        返回 None 表示无法推断（使用默认配置）。
        """
        if not filename:
            return None
        
        basename = os.path.splitext(os.path.basename(filename))[0].lower()
        model_lower = model_display.lower() if model_display else ""
        
        # ===== Step 1: 使用用户配置的歌手性别映射表 =====
        song_gender = None
        for artist, gender in self.artist_gender_map.items():
            if artist.lower() in basename:
                song_gender = gender
                logger.info(f"[AutoKeyShift] 文件名匹配歌手映射表: '{artist}' → {gender}")
                break
        
        # ===== Step 2: 如果映射表未命中，用关键词推断 =====
        if not song_gender:
            male_indicators = [
                "周杰伦", "林俊杰", "陈奕迅", "李荣浩", "薛之谦", "毛不易", "张学友",
                "刘德华", "周深", "五月天", "beyond", "陶喆", "方大同", "王力宏",
                "萧敬腾", "杨宗纬", "张敬轩", "胡彦斌", "许嵩", "汪苏泷",
                "听妈妈的话", "晴天", "稻香", "七里香", "青花瓷", "告白气球",
                "夜曲", "简单爱", "不能说的秘密", "蒲公英的约定", "一路向北",
                "江南", "曹操", "背对背拥抱", "可惜没如果", "修炼爱情",
                "十年", "浮夸", "k歌之王", "富士山下",
                "模特", "年少有为", "不将就", "戒烟",
                "演员", "绅士", "认真的雪", "刚刚好",
                "消愁", "像我这样的人", "借", "无问",
                "海阔天空", "光辉岁月", "真的爱你", "喜欢你",
                "普通朋友", "小镇姑娘", "流沙",
                "爱爱爱", "三人游", "特别的人",
                "唯一", "爱你等于爱自己", "龙的传人",
            ]
            female_indicators = [
                "邓紫棋", "王菲", "蔡依林", "张惠妹", "孙燕姿", "田馥甄",
                "梁静茹", "刘若英", "林忆莲", "蔡健雅", "徐佳莹", "周笔畅",
                "张靓颖", "李宇春", "谭维维", "萨顶顶", "阿兰",
                "霉霉", "taylor", "adele", "beyonce", "rihanna",
                "光年之外", "泡沫", "喜欢你", "句号", "倒数",
                "红豆", "传奇", "如愿", "匆匆那年",
                "日不落", "舞娘", "说爱你", "看我72变",
                "听海", "我可以抱你吗", "记得",
                "遇见", "天黑黑", "开始懂了", "我怀念的",
                "小幸运", "你就不要想起我", "寂寞寂寞就好",
                "勇气", "暖暖", "分手快乐", "宁夏",
                "后来", "很爱很爱你", "成全",
            ]
            is_male_song = any(ind.lower() in basename for ind in male_indicators)
            is_female_song = any(ind.lower() in basename for ind in female_indicators)
            if is_male_song:
                song_gender = "male"
            elif is_female_song:
                song_gender = "female"
        
        # 如果仍无法推断歌曲性别，返回 None
        if not song_gender:
            return None
        
        # ===== Step 3: 推断模型性别（优先用户配置的模型性别映射表）=====
        model_gender = None
        for alias, gender in self.model_gender_map.items():
            if alias.lower() in model_lower:
                model_gender = gender
                logger.info(f"[AutoKeyShift] 模型匹配映射表: '{alias}' → {gender}")
                break
        
        # 映射表未命中，用关键词推断
        if not model_gender:
            female_model_keywords = ["甘城", "猫猫", "塔菲", "tafei", "nyaa", "miku", "初音", "镜音", "洛天依", "reol", "ado", "yoasobi", "yorushika", "ayase", "ikura", " LiSA", "美波", "aimer", "花谱", "可不", "星界", "星尘", "心华", "乐正", "墨清弦", "言和", "战音", "绯村", "鹿乃", "hanser", "泠鸢", "冰糖", "七海", "阿梓", "小可", "永雏", "猫雷", "東雪蓮", "明前", "兰音", "扇宝", "虚研", "四禧", "丸子", "EOE", "ASOUL", "乃琳", "贝拉", "珈乐", "嘉然", "向晚", "星瞳", "雫lulu", "真白花音", "早稻叽", "白神遥", "花园", "hiiro", "希月", "奈奈", "米诺", "虞莫", "莞儿", "露早", "柚恩"]
            male_model_keywords = ["kano", "shinichi", "男声", "少年", "大叔", "青年", "正太", "低音", "王爷", "葛平", "波澜", "面筋", "元首", "哲♂学", "比利", "van样", "香蕉君", "刘醒", "梁非凡", "金坷垃", "庞麦郎", "波澜哥", "李云龙", "张涵予", "姜文", "于谦", "郭德纲", "赵本山", "范伟", "沈腾", "黄渤", "徐峥", "王宝强", "刘烨", "张译", "朱亚文", "靳东", "胡歌", "王凯", "肖战", "王一博", "易烊千玺", "王俊凯", "王源", "华晨宇", "毛不易", "周深", "张杰", "薛之谦", "李荣浩", "许嵩", "汪苏泷", "陈奕迅", "林俊杰", "周杰伦"]
            if any(kw in model_lower for kw in female_model_keywords):
                model_gender = "female"
            elif any(kw in model_lower for kw in male_model_keywords):
                model_gender = "male"
        
        # 如果无法推断模型性别，假设为女声模型（大多数AI翻唱模型偏女声）
        if not model_gender:
            model_gender = "female"
        
        # ===== Step 4: 根据性别匹配关系返回配置中的数值 =====
        if song_gender == model_gender:
            return 0  # 同性匹配，保持原调
        elif model_gender == "female" and song_gender == "male":
            return self.male_to_female_shift  # 男→女，使用配置的升调值
        elif model_gender == "male" and song_gender == "female":
            return self.female_to_male_shift  # 女→男，使用配置的降调值
        
        return None

    async def _cleanup_expired_audio_cache(self):
        """清理过期的近期音频缓存"""
        now = time.time()
        expired = []
        async with self._audio_cache_lock:
            for key, (path, ts) in list(self._recent_audio_cache.items()):
                if now - ts > self._recent_audio_cache_ttl:
                    expired.append(key)
                    temp_dir = _temp_audio_dir()
                    if path.startswith(temp_dir) and os.path.isfile(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            for key in expired:
                self._recent_audio_cache.pop(key, None)

    async def _extract_audio_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        """从消息事件中提取音频文件的本地路径"""
        msg_chain = getattr(event.message_obj, "message", [])
        if not msg_chain:
            logger.debug("[LocalAudio] 消息链为空")
            return None
        
        for idx, comp in enumerate(msg_chain):
            comp_type = type(comp).__name__
            logger.debug(f"[LocalAudio] 检查消息组件 {idx}: {comp_type}")
            
            if isinstance(comp, Record):
                file_path = getattr(comp, "file_", None) or getattr(comp, "file", None)
                file_url = getattr(comp, "url", None)
                file_name = getattr(comp, "name", "")
                
                logger.info(f"[LocalAudio] 发现语音/音频消息: file={file_path}, url={file_url}")
                
                # 优先使用本地路径
                if file_path and os.path.isfile(file_path):
                    return file_path
                
                # 尝试 URL 直接下载
                if file_url:
                    # 尝试从 url 或 filename 提取后缀
                    suffix = ".mp3"
                    if file_name and "." in file_name:
                        suffix = os.path.splitext(file_name)[1]
                    elif file_url and "." in file_url.split("?")[0]:
                        url_ext = os.path.splitext(file_url.split("?")[0])[1]
                        if len(url_ext) <= 5: # 确保是正常的后缀
                            suffix = url_ext
                            
                    downloaded = await self._download_audio(file_url, suffix)
                    if downloaded:
                        return downloaded
                
                # 尝试通过 OneBot API 获取
                if file_path:
                    try:
                        record_info = await event.bot.call_action("get_record", file=file_path, out_format="mp3")
                        logger.info(f"[LocalAudio] get_record 返回: {record_info}")
                        if isinstance(record_info, dict):
                            actual_path = record_info.get("file") or record_info.get("path")
                            if actual_path and os.path.isfile(actual_path):
                                return actual_path
                            actual_url = record_info.get("url")
                            if actual_url:
                                downloaded = await self._download_audio(actual_url, ".mp3")
                                if downloaded:
                                    return downloaded
                    except Exception as e:
                        logger.warning(f"[LocalAudio] 获取语音文件失败: {e}")
            
            elif isinstance(comp, File):
                file_path = getattr(comp, "file_", None) or getattr(comp, "file", None)
                file_name = getattr(comp, "name", "") or ""
                file_url = getattr(comp, "url", None)
                logger.info(f"[LocalAudio] 发现文件消息: name={file_name}, file={file_path}, url={file_url}")
                
                if not self._is_audio_file(file_name):
                    logger.debug(f"[LocalAudio] 跳过非音频文件: {file_name}")
                    continue
                
                # 优先本地路径
                if file_path and os.path.isfile(file_path):
                    size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    if size_mb > self.max_local_audio_size_mb:
                        logger.warning(f"[LocalAudio] 音频文件过大: {size_mb:.1f}MB")
                        return None
                    return file_path
                
                # 尝试 URL 直接下载
                if file_url:
                    downloaded = await self._download_audio(file_url, os.path.splitext(file_name)[1] or ".mp3")
                    if downloaded:
                        return downloaded
                
                # 尝试通过 OneBot API 获取文件
                if file_path:
                    try:
                        file_info = await event.bot.call_action("get_file", file_id=file_path)
                        logger.info(f"[LocalAudio] get_file 返回: {file_info}")
                        if isinstance(file_info, dict):
                            actual_path = file_info.get("file") or file_info.get("path")
                            if actual_path and os.path.isfile(actual_path):
                                size_mb = os.path.getsize(actual_path) / (1024 * 1024)
                                if size_mb > self.max_local_audio_size_mb:
                                    logger.warning(f"[LocalAudio] 音频文件过大: {size_mb:.1f}MB")
                                    return None
                                return actual_path
                            actual_url = file_info.get("url")
                            if actual_url:
                                downloaded = await self._download_audio(actual_url, os.path.splitext(file_name)[1] or ".mp3")
                                if downloaded:
                                    return downloaded
                    except Exception as e:
                        logger.warning(f"[LocalAudio] 通过API获取文件失败: {e}")
        
        logger.info(f"[LocalAudio] 未从消息中提取到音频。消息链组件数: {len(msg_chain)}")
        return None

    async def _download_audio(self, url: str, suffix: str = ".mp3") -> Optional[str]:
        """下载音频文件到临时目录"""
        try:
            import aiohttp
            temp_dir = _temp_audio_dir()
            os.makedirs(temp_dir, exist_ok=True)
            dest = os.path.join(temp_dir, f"dl_{uuid.uuid4().hex[:8]}{suffix}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        max_bytes = int(float(self.max_local_audio_size_mb) * 1024 * 1024)
                        content_length = int(resp.headers.get("Content-Length") or 0)
                        if content_length > max_bytes:
                            logger.warning(
                                f"下载的音频过大: {content_length / 1024 / 1024:.1f}MB，已拒绝"
                            )
                            return None
                        written = 0
                        with open(dest, "wb") as f:
                            async for chunk in resp.content.iter_chunked(8192):
                                written += len(chunk)
                                if written > max_bytes:
                                    raise ValueError(
                                        f"下载的音频超过 {self.max_local_audio_size_mb}MB 限制"
                                    )
                                f.write(chunk)
                        size_mb = os.path.getsize(dest) / (1024 * 1024)
                        if size_mb > self.max_local_audio_size_mb:
                            logger.warning(f"下载的音频过大: {size_mb:.1f}MB，已丢弃")
                            os.remove(dest)
                            return None
                        return dest
                    else:
                        logger.warning(f"下载音频失败: HTTP {resp.status}")
        except Exception as e:
            if 'dest' in locals() and os.path.isfile(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            logger.error(f"下载音频异常: {e}")
        return None

    # ==================== 网易云取歌（0.5.0 起照搬点歌插件 astrbot_plugin_music） ====================

    def _netease_temp_dir(self) -> str:
        """插件自己的临时音频目录（与本地音频下载共用，退出时会清理）。"""
        temp_dir = _temp_audio_dir()
        os.makedirs(temp_dir, exist_ok=True)
        return temp_dir

    def _netease_quality_br(self) -> int:
        """配置里的音质（128/192/320）换算成接口要的码率。"""
        return int(self.netease_quality) * 1000


    def _short_failure_reason(self, detail: str) -> str:
        """把技术性失败详情压成一句人话（批量报告用，细节只进日志）。"""
        text = str(detail or "")
        if any(word in text for word in ("音源", "取歌", "搜不到", "未找到", "拿不到")):
            return "这首拿不到音源"
        if "超时" in text:
            return "生成超时了"
        if "禁用" in text:
            return "RVC 没开"
        if "模型" in text and ("没有可用" in text or "序号" in text):
            return "没有可用的音色"
        if any(word in text for word in ("生成失败", "未返回有效", "没出成品")):
            return "后端没出成品"
        if "取消" in text:
            return "被取消了"
        return "这首没做成"

    def _get_admin_ids(self) -> List[str]:
        """从 AstrBot 全局配置读管理员 QQ 号（插件自己的 config 里没有）。"""
        try:
            global_config = self.context.get_config()
        except Exception as exc:
            logger.warning(f"读取 AstrBot 全局配置失败，无法拿到管理员: {exc}")
            return []
        admin_ids = None
        try:
            admin_ids = global_config.get("admins_id", [])
        except Exception as exc:
            logger.warning(f"全局配置里读 admins_id 失败: {exc}")
            return []
        if isinstance(admin_ids, str):
            admin_ids = [admin_ids]
        result: List[str] = []
        for item in admin_ids or []:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _friend_session(umo: str, user_id: str) -> str:
        """由当前事件的 umo 推出"同一平台、私聊某个 QQ"的 session。"""
        platform = str(umo or "").split(":", 1)[0].strip()
        if not platform or not str(user_id or "").strip():
            return ""
        return f"{platform}:FriendMessage:{str(user_id).strip()}"

    def _cookie_alert_target(self, event: AstrMessageEvent):
        """提醒管理员时发到哪：优先管理员私聊，找不到管理员就发回触发点歌的会话。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        for admin_id in self._get_admin_ids():
            session = self._friend_session(umo, admin_id)
            if session:
                logger.info(f"网易云 cookie 失效提醒将私聊管理员: {session}")
                return _OutboundTarget(self.context, session, umo)
        logger.warning("找不到管理员 admins_id，cookie 失效提醒改发到触发点歌的会话")
        return event

    def _reset_netease_cookie_state(self) -> None:
        """取歌成功后清空"连续失败"和提醒去重状态。"""
        if self._netease_cookie_fail_streak or self._cookie_alert_sent_at:
            logger.info("网易云取歌已恢复，清空 cookie 失效状态")
        self._netease_cookie_fail_streak = 0
        self._cookie_alert_sent_at = 0.0

    async def _notify_cookie_expired(self, event: AstrMessageEvent, reason: str) -> None:
        """连续 3 次 cookie 相关失败后提醒管理员；同一失效状态按间隔去重。"""
        if not self.alert_admin_on_cookie_expired:
            return
        now = time.time()
        interval_seconds = max(1, int(self.cookie_alert_interval_hours)) * 3600
        if self._cookie_alert_sent_at and now - self._cookie_alert_sent_at < interval_seconds:
            logger.info(
                "网易云 cookie 失效提醒在去重窗口内，跳过 "
                f"（{int((now - self._cookie_alert_sent_at) / 60)} 分钟前已提醒过）"
            )
            return
        facts = {
            "reason": "网易云的 cookie 好像失效了，取不到整首歌",
            "detail": (
                f"连续 {self._netease_cookie_fail_streak} 次取歌都因为这个失败，"
                f"最近一次：{reason}"
            ),
        }
        target = self._cookie_alert_target(event)
        sent = await self.outbound.say(target, "alert", **facts)
        if not sent and target is not event:
            # 管理员会话发不出去（平台/session 对不上）时，退回触发点歌的会话
            logger.warning("管理员会话发送失败，cookie 失效提醒改发到触发点歌的会话")
            sent = await self.outbound.say(event, "alert", **facts)
        if sent:
            # 只有真发出去了才占用去重窗口
            self._cookie_alert_sent_at = now
        else:
            logger.warning("cookie 失效提醒一条都没发出去，下次失败会再试")

    async def _record_netease_cookie_failure(
        self, event: AstrMessageEvent, reason: str, is_trial: bool
    ) -> None:
        """记一次 cookie 相关失败；连续 3 次才提醒，非 cookie 失败打断连续计数。"""
        if not netease_fetch.is_cookie_failure(reason, is_trial):
            if self._netease_cookie_fail_streak:
                logger.info(
                    f"网易云取歌失败原因不是 cookie（{reason}），连续失败计数清零"
                )
            self._netease_cookie_fail_streak = 0
            return
        self._netease_cookie_fail_streak += 1
        logger.warning(
            f"网易云 cookie 相关失败第 {self._netease_cookie_fail_streak} 次: "
            f"reason={reason} trial={is_trial}"
        )
        if self._netease_cookie_fail_streak >= 3:
            await self._notify_cookie_expired(event, reason)

    # ==================== 统一取歌入口（0.7.0：多音源路由） ====================


    @staticmethod
    def _song_identity(song: Any) -> tuple[str, str, str]:
        """从歌曲对象里取出 ``(id/mid, 歌名, 歌手)``，兼容 dict 和裸字符串。

        卡片 / 命令选歌传的是 dict（有 name / artists / id / songmid），
        LLM 工具和批量翻唱传的是歌名裸字符串，两种都要能走同一个入口。
        """
        if isinstance(song, str):
            text = song.strip()
            return text, text, ""
        if not isinstance(song, dict):
            text = str(song or "").strip()
            return text, text, ""
        raw_id = str(song.get("songmid") or song.get("id") or "").strip()
        name = str(song.get("name") or song.get("title") or "").strip()
        artists = str(song.get("artists") or song.get("artist") or "").strip()
        return raw_id, name, artists


    @staticmethod
    def _pick_best_song(songs: list, query: str, artist: str = "") -> dict:
        """在搜索结果里挑**最确定**的一首；一个都不像就返回空 dict。

        0.8.0 起匹配统一走 ``source_router.pick_best_song``：歌名整串原样搜、
        别名只按「标题+别名」拼接比较、伴奏类先淘汰、歌手只在明确给出时当过滤条件；
        没有任何打分或模糊规则。这里保留这个入口，只是把 dict 列表转成候选再转回来。
        """
        return source_router.pick_best_song_dict(songs, query, artist)

    @staticmethod
    def _drop_file(path: str) -> None:
        """删掉一个临时文件；删不掉只记日志，不影响主流程。"""
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.debug(f"删除临时文件失败: {path} ({exc})")

    def _is_temp_audio(self, path: str) -> bool:
        """判断文件是不是插件自己下到 ``temp_audio/`` 的（只有这种才在 finally 删）。"""
        try:
            temp_dir = os.path.abspath(self._netease_temp_dir())
            return os.path.abspath(str(path)).startswith(temp_dir + os.sep)
        except Exception:
            return False

    async def _prepare_song_audio(
        self, event: AstrMessageEvent, song: Any
    ) -> tuple[Optional[str], str, bool]:
        """统一取歌入口：把"歌曲对象 / 歌名 / 歌曲 id"变成本地音频文件。

        0.7.0 起所有取歌路径都交给 :class:`source_router.SourceRouter`：

        1. 已经是存在的本地文件 → 直接返回，不联网；
        2. 其它情况交给路由器按 ``source_order`` 依次尝试各平台：
           搜歌（song_name 整串）→ 标题/标题+别名拼接匹配 → 排除伴奏类
           → 取播放链接 → 下载 → 80 秒闸门；
        3. 前一个平台搜不到 / 选不出 / 下不下来就自动换下一个，日志里每个平台一行；
        4. 全部失败时把每个平台的原因串起来返回；如果所有候选都只是伴奏版，
           原因就是「只有伴奏版」。

        Returns:
            ``(本地文件路径 或 None, 失败原因, 是否试听)``。失败原因是简短中文，
            堆栈和技术细节只进 logger。
        """
        raw_id, name, artists = self._song_identity(song)
        label = name or raw_id or "这首歌"
        source_hint = ""
        if isinstance(song, dict):
            source_hint = str(song.get("source") or "").strip().casefold()
        # 酷我 / QQ 的 id 也是纯数字，不能把别家来源的数字 id 当网易云 id 用
        if raw_id.isdigit() and source_hint not in ("", "netease"):
            source_hint = ""

        # 1) 已经是本地文件：直接交给中间层，别再搜一遍
        for candidate in (raw_id, name):
            if candidate and os.path.isfile(candidate):
                logger.info(f"[取歌] {label} 已经是本地文件，直接使用: {candidate}")
                return candidate, "", False

        # 2) 标题/别名带伴奏标记的，明确选了也不拿去翻唱（会出静音）
        alias_text = ""
        if isinstance(song, dict):
            alias_text = str(song.get("alias") or "").strip()
        if source_router.is_accompaniment(name, alias_text):
            logger.info(f"[取歌] {label} 标题/别名带伴奏标记，判为没有人声")
            return None, source_router.REASON_ONLY_ACCOMPANIMENT, False

        if not raw_id and not name:
            logger.info("[取歌] 既没有 id 也没有歌名，放弃")
            return None, "没有歌名也没有 id", False

        await self._send_progress_notice(
            event, step="正在按顺序找音源", song=label
        )
        result = await self.song_router.fetch_audio(
            {
                "id": raw_id,
                "name": name or raw_id,
                "artists": artists,
                "source": source_hint,
            },
            dest_dir=self._netease_temp_dir(),
        )

        # 网易云那条失败照旧计入 cookie 失效提醒（其它源失败不掺和）
        for attempt in result.attempts:
            if attempt.source == "netease" and not attempt.ok:
                await self._record_netease_cookie_failure(
                    event, attempt.reason, attempt.is_trial
                )

        if result.path:
            self._reset_netease_cookie_state()
            candidate = result.candidate
            logger.info(
                f"[取歌] 整首取到: 来源={result.source} "
                f"id={candidate.id if candidate else '?'} "
                f"歌名={candidate.name if candidate else label} "
                f"时长={(candidate.duration_ms / 1000) if candidate else 0:.1f} 秒 "
                f"file={result.path}"
            )
            return result.path, "", False

        logger.info(f"[取歌] {label} 所有音源都没拿到: {result.reason}")
        return None, result.reason, result.is_trial


    def _create_tracked_task(self, coro) -> asyncio.Task:
        """创建被追踪的异步任务，防止任务丢失和异常静默"""
        coro_name = getattr(getattr(coro, "cr_code", None), "co_name", "background")
        task = asyncio.create_task(coro, name=f"rvc_console:{coro_name}")
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        logger.info(
            f"[任务Debug] 已创建后台任务: {task.get_name()} | "
            f"pending={len(self._pending_tasks)}"
        )

        def _log_exception(t: asyncio.Task):
            if t.cancelled():
                logger.warning(f"[任务Debug] 后台任务已取消: {t.get_name()}")
            elif t.exception() is not None:
                logger.error(f"[RVC控制台] 异步任务异常: {t.exception()}", exc_info=t.exception())
            else:
                logger.info(f"[任务Debug] 后台任务已完成: {t.get_name()}")

        task.add_done_callback(_log_exception)
        return task
    
    @asynccontextmanager
    async def _get_gradio_client(self, base_url: str):
        """安全获取Gradio Client，确保正确关闭"""
        client = None
        try:
            # Client 初始化会同步读取 /config；放到线程池，避免阻塞 AstrBot 事件循环。
            client = await asyncio.to_thread(
                Client, base_url, verbose=False, analytics_enabled=False
            )
            yield client
        finally:
            if client is not None:
                try:
                    await asyncio.to_thread(client.close)
                except Exception as e:
                    logger.debug(f"关闭Gradio Client时出错: {e}")

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识"""
        return str(event.get_sender_id())

    def _get_user_pref(self, user_id: str) -> Dict:
        """获取用户偏好，如果不存在则创建默认值"""
        if user_id not in self.user_preferences:
            self.user_preferences[user_id] = {
                "model_index": FALLBACK_MODEL_INDEX,
                "default_key_shift": self.default_key_shift,
                "favorite_songs": [],
                "usage_count": 0,
                "last_used_time": None,
                "preferred_artists": {},
                "artist_model_map": {}
            }
        pref = self.user_preferences[user_id]
        if isinstance(pref.get("preferred_artists"), list):
            old_list = pref["preferred_artists"]
            pref["preferred_artists"] = {a: {"count": 1, "last_time": None, "model": None} for a in old_list}
        if "artist_model_map" not in pref:
            pref["artist_model_map"] = {}
        if pref.get("model_index", 0) < 1:
            pref["model_index"] = FALLBACK_MODEL_INDEX
        return pref

    async def _async_predict(self, client, *args, timeout=300, event=None, detect_cache_hit=False, **kwargs):
        loop = asyncio.get_running_loop()
        job = await asyncio.to_thread(client.submit, *args, **kwargs)
        self._bind_active_gradio_job(event, job)
        timeout_seconds = max(0.1, float(timeout))
        deadline = time.monotonic() + timeout_seconds

        def cancel_job_best_effort():
            """Ask Gradio to stop queued/running work without masking the timeout."""
            try:
                cancel = getattr(job, "cancel", None)
                if callable(cancel):
                    cancel()
            except Exception as cancel_error:
                logger.debug(f"推理超时后取消 Gradio 任务失败: {cancel_error}")

        def raise_inference_timeout(cause=None):
            cancel_job_best_effort()
            message = (
                f"推理任务在 {timeout_seconds:g} 秒内未完成，"
                "已尝试取消后端任务，请检查推理服务状态或调大任务超时时间。"
            )
            if cause is None:
                raise TimeoutError(message)
            raise TimeoutError(message) from cause
        
        cache_hit_detected = False

        # 0.10.1：原先这里有一套「直接发 QQ 进度条」的分支（enable_progress_bar /
        # progress_update_interval），它绕过 outbound 统一出口、发固定 emoji 文案，
        # 与 notify_progress 重复。设置页已删除这两个开关，这里也一并去掉：
        # 进度只走 outbound（默认静音），超时由下面的 deadline 逻辑兜住。

        remaining = deadline - time.monotonic()
        if remaining <= 0 and not job.done():
            raise_inference_timeout()

        # A completed Gradio job returns immediately.  Otherwise both
        # job.result() and asyncio.wait_for() share the same monotonic budget.
        result_timeout = max(0.0, remaining) if not job.done() else 0
        fn = partial(job.result, timeout=result_timeout)
        result_future = loop.run_in_executor(None, fn)
        try:
            if job.done():
                result = await result_future
            else:
                result = await asyncio.wait_for(result_future, timeout=remaining)
        except TimeoutError as exc:
            raise_inference_timeout(exc)
        
        # 解析中间层返回的缓存标记（元组格式: (file_path, "true"/"false")）
        cache_hit_from_api = False
        if detect_cache_hit and isinstance(result, (list, tuple)) and len(result) >= 2:
            cache_flag = str(result[1]).lower()
            cache_hit_from_api = cache_flag == "true"
            result = result[0]
        
        # 如果之前未检测到缓存命中，再尝试从最终状态确认（备用方案）
        if detect_cache_hit and not cache_hit_detected and not cache_hit_from_api:
            try:
                final_status = job.status()
                if hasattr(final_status, 'progress_data') and final_status.progress_data:
                    for pd in final_status.progress_data:
                        desc = getattr(pd, 'desc', None)
                        if desc is None and isinstance(pd, dict):
                            desc = pd.get('desc')
                        if desc and ('cache hit' in desc.lower() or '缓存命中' in desc):
                            cache_hit_detected = True
                            break
            except Exception as e:
                logger.debug(f"最终状态检测缓存时出错: {e}")
        
        if detect_cache_hit:
            self._clear_active_gradio_job(event, job)
            return result, cache_hit_from_api or cache_hit_detected
        self._clear_active_gradio_job(event, job)
        return result

    def _queue_snapshot(self) -> dict:
        """当前任务与排队快照，供 WebUI「当前任务」使用。"""
        now = time.time()
        active = None
        if self._cover_active:
            active = dict(self._cover_active)
            active["elapsed"] = round(now - float(active.get("at") or now), 1)
        waiting = []
        for idx, item in enumerate(self._cover_queue, start=1):
            row = dict(item)
            row["position"] = idx
            row["waited"] = round(now - float(row.get("at") or now), 1)
            waiting.append(row)
        return {"active": active, "waiting": waiting,
                "limit": int(getattr(self, "cover_queue_limit", 3))}

    def _gateway_base_url(self) -> str:
        """CUSTOM-9:网关地址实时读配置(WebUI 改完即生效,支持任意主机/端口)。"""
        try:
            return str(self.config.get("rvc_base_url", "") or "").strip() or "http://127.0.0.1:3333/"
        except Exception:  # noqa: BLE001
            return "http://127.0.0.1:3333/"

    def _gateway_work_dir(self) -> str:
        """网关工作目录（宿主机路径），来自配置 `gateway_work_dir`；未配置返回空串。"""
        try:
            return str(self.config.get("gateway_work_dir", "") or "").strip()
        except Exception:
            return ""

    def _gateway_out_dir(self) -> str:
        gw = self._gateway_work_dir()
        return os.path.join(gw, "out") if gw else ""

    def apply_live_config(self, changed_keys) -> None:
        """CUSTOM-9：设置保存后同步运行时缓存属性（WebUI 改完即生效，无需重载）。

        TTS 触发/参数等已是实时读取；这里同步仍以属性缓存的字段。
        """
        cfg = self.config
        try:
            for key in changed_keys or []:
                if key == "enable_rvc":
                    self.enable_rvc = bool(cfg.get("enable_rvc", True))
                elif key == "llm_force_mode":
                    self.llm_force_mode = bool(cfg.get("llm_force_mode", False))
                elif key == "rvc_default_model":
                    self.rvc_default_model = str(cfg.get("rvc_default_model", "") or "").strip()
                elif key == "inference_timeout":
                    self.inference_timeout = max(30, int(cfg.get("inference_timeout", 600) or 600))
                elif key == "tts_timeout":
                    self.tts_timeout = max(60, int(cfg.get("tts_timeout", 300) or 300))
                elif key == "cover_queue_limit":
                    self.cover_queue_limit = max(0, min(10, int(cfg.get("cover_queue_limit", 3))))
                elif key == "tts_queue_limit":
                    self.tts_queue_limit = max(0, min(10, int(cfg.get("tts_queue_limit", 3))))
                elif key == "task_queue_limit":
                    self.tasks.max_pending = max(1, int(cfg.get("task_queue_limit", 10) or 10))
                elif key == "duplicate_window_seconds":
                    self.tasks.duplicate_window = max(0.0, float(cfg.get("duplicate_window_seconds", 120) or 0))
                elif key == "max_batch_size":
                    self.max_batch_size = int(cfg.get("max_batch_size", 5) or 5)
                elif key == "task_timeout_seconds":
                    self.task_timeout_seconds = int(cfg.get("task_timeout_seconds", 9000) or 9000)
                elif key == "chorus_seconds":
                    self.chorus_seconds = max(20, min(180, int(cfg.get("chorus_seconds", 45) or 45)))
                elif key == "render_mode":
                    rm = str(cfg.get("render_mode", "chorus") or "chorus").strip().lower()
                    self.render_mode = rm if rm in ("chorus", "full", "custom") else "chorus"
                elif key == "rvc_models_keywords":
                    self.rvc_models_keywords = list(cfg.get("rvc_models_keywords", []) or [])
                elif "model_gender" in key:
                    # 模型管理页统一保存提交的是完整路径键
                    # （tuning.gender.model_genders[_extra]），一并刷新性别映射
                    self.model_gender_map = self._load_model_genders(cfg)
                    logger.info("[RVC控制台] 模型性别已实时更新: %s", self.model_gender_map)
                elif key == "model_genders":
                    self.model_gender_map = self._load_model_genders(cfg)
                    logger.info("[RVC控制台] 模型性别已实时更新: %s", self.model_gender_map)
                elif key == "enable_task_tracking":
                    self.enable_task_tracking = bool(cfg.get("enable_task_tracking", True))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[RVC控制台] 运行时配置同步失败（不影响已保存值）: {exc}")

    def _set_cover_stage(self, stage: str) -> None:
        if self._cover_active is not None:
            self._cover_active["stage"] = stage
            self._cover_active["updated"] = time.time()

    async def _acquire_cover_slot(self, umo: str, label: str,
                                  meta: Optional[dict] = None) -> bool:
        """占用一个推理槽位；排队已满返回 False。"""
        busy = self._cover_slot.locked() or bool(self._cover_queue)
        if busy:
            if len(self._cover_queue) >= int(getattr(self, "cover_queue_limit", 3)):
                return False
            entry = {"umo": umo, "label": label, "at": time.time(), "stage": "排队中"}
            if meta:
                entry.update({k: v for k, v in meta.items() if v})
            self._cover_queue.append(entry)
            logger.info("[RVC控制台] 加入队列: %s（前面还有 %d 个）",
                        label, len(self._cover_queue) - 1)
            try:
                await self._cover_slot.acquire()
            finally:
                try:
                    self._cover_queue.remove(entry)
                except ValueError:
                    pass
        else:
            await self._cover_slot.acquire()
        self._cover_active = {"umo": umo, "label": label, "at": time.time(),
                              "stage": "人声分离与变声"}
        if meta:
            self._cover_active.update({k: v for k, v in meta.items() if v})
        return True

    def _release_cover_slot(self) -> None:
        self._cover_active = None
        if self._cover_slot.locked():
            self._cover_slot.release()

    # ==================== CUSTOM-7.4：TTS 队列（与翻唱队列同构） ====================

    def _set_tts_stage(self, stage: str) -> None:
        if self._tts_active is not None:
            self._tts_active["stage"] = stage
            self._tts_active["updated"] = time.time()

    def _tts_queue_snapshot(self) -> dict:
        """当前 TTS 任务与排队快照，供 WebUI「任务」页使用。"""
        now = time.time()
        active = None
        if self._tts_active:
            active = dict(self._tts_active)
            active["elapsed"] = round(now - float(active.get("at") or now), 1)
        waiting = []
        for idx, item in enumerate(self._tts_queue, start=1):
            row = dict(item)
            row["position"] = idx
            row["waited"] = round(now - float(row.get("at") or now), 1)
            waiting.append(row)
        return {"active": active, "waiting": waiting,
                "limit": int(getattr(self, "tts_queue_limit", 3))}

    async def _acquire_tts_slot(self, umo: str, label: str,
                                meta: Optional[dict] = None) -> bool:
        """占用 TTS 槽位；排队已满返回 False。"""
        busy = self._tts_slot.locked() or bool(self._tts_queue)
        if busy:
            if len(self._tts_queue) >= int(getattr(self, "tts_queue_limit", 3)):
                return False
            entry = {"umo": umo, "label": label, "at": time.time(), "stage": "排队中"}
            if meta:
                entry.update({k: v for k, v in meta.items() if v})
            self._tts_queue.append(entry)
            logger.info("[TTS] 加入队列: %s（前面还有 %d 个）",
                        label, len(self._tts_queue) - 1)
            try:
                await self._tts_slot.acquire()
            finally:
                try:
                    self._tts_queue.remove(entry)
                except ValueError:
                    pass
        else:
            await self._tts_slot.acquire()
        self._tts_active = {"umo": umo, "label": label, "at": time.time(),
                            "stage": "TTS 生成与变声"}
        if meta:
            self._tts_active.update({k: v for k, v in meta.items() if v})
        return True

    def _release_tts_slot(self) -> None:
        self._tts_active = None
        if self._tts_slot.locked():
            self._tts_slot.release()

    def _record_tts_history(self, task_id: str, user: str, request_text: str,
                            speak_text: str, model: str, output_path: str,
                            duration: Any = None, user_name: str = "",
                            group_id: str = "", group_name: str = "") -> None:
        """TTS 成品写入 tts_history.json（≤300 条，供 WebUI 任务列表显示/回放）。"""
        try:
            path = os.path.join(_plugin_data_dir(), "tts_history.json")
            data = {}
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            data[task_id] = {
                "task_id": task_id,
                "user": str(user or "WebUI"),
                "user_name": str(user_name or "")[:60],
                "group_id": str(group_id or "")[:40],
                "group_name": str(group_name or "")[:60],
                "request": str(request_text or "")[:200],
                "speak": str(speak_text or "")[:200],
                "model": str(model or ""),
                "output": str(output_path or ""),
                "duration": duration,
                "created_at": time.time(),
            }
            items = sorted(data.items(),
                           key=lambda kv: kv[1].get("created_at", 0), reverse=True)
            data = dict(items[:300])
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[TTS] 记录 TTS 历史失败: {exc}")

    async def _predict_cover(
        self,
        event: AstrMessageEvent,
        song_name_src: str,
        model_name: str,
        key_shift: Optional[int],
    ) -> tuple[Any, bool]:
        """排队后提交给 RVC 中间层（同一时间只跑一个，其余排队）。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        label = os.path.basename(str(song_name_src)) or "音频"
        meta = dict(self._pending_cover_meta.pop(umo, None) or {})
        meta["registry_id"] = str(getattr(event, "_cover_registry_id", "") or "")
        ahead = len(self._cover_queue) + (1 if self._cover_slot.locked() else 0)
        if not await self._acquire_cover_slot(umo, label, meta=meta):
            raise RuntimeError(
                f"当前排队已满（上限 {int(getattr(self, 'cover_queue_limit', 3))} 人），请稍后再点")
        try:
            # CUSTOM-8：登记进统一注册表的任务进入运行态；排队位置通知
            try:
                reg_id = str(getattr(event, "_cover_registry_id", "") or "")
                if reg_id:
                    self.tasks.begin_by_id(reg_id)
            except Exception:  # noqa: BLE001
                pass
            if ahead > 0 and bool(self.config.get("queue_position_notice", True)):
                try:
                    await event.send(event.plain_result(f"已加入队列，前面还有 {ahead} 个任务。"))
                except Exception:  # noqa: BLE001
                    pass
            self._set_cover_stage("推理中（CPU 分离 + 变声）")
            return await self._predict_cover_impl(
                event=event,
                song_name_src=song_name_src,
                model_name=model_name,
                key_shift=key_shift,
            )
        finally:
            self._release_cover_slot()

    async def _predict_cover_impl(
        self,
        event: AstrMessageEvent,
        song_name_src: str,
        model_name: str,
        key_shift: Optional[int],
    ) -> tuple[Any, bool]:
        """把一首（或一段）音频提交给 RVC 中间层，等它返回成品路径。"""
        base_url = self._gateway_base_url()
        async with self._get_gradio_client(base_url) as client:
            msst_kwargs = await self._get_optional_msst_kwargs(client, base_url)
            render_kwargs = self._get_render_kwargs(client)
            mix_kwargs = self._get_mix_kwargs(client)
            fade_kwargs = self._get_fade_kwargs(client)
            reverb_kwargs = self._get_reverb_kwargs(client)
            # CUSTOM-9：推理参数实时读配置（WebUI 改完即生效，无需重载）
            legacy_kwargs = {
                "song_name_src": song_name_src,
                "key_shift": int(key_shift or 0),
                "vocal_vol": 0,
                "inst_vol": 0,
                "model_dropdown": model_name,
                "shift_accompaniment": self.config.get("shift_accompaniment", True),
                "f0_method": str(self.config.get("f0_method", "rmvpe") or "rmvpe"),
                "index_rate": self.config.get("index_rate", 0.75),
                # 0.10.1：reverb_intensity / delay_intensity / vocal_postprocess /
                # filter_radius / uvr5_* / msst_batch_size 等已从设置页删除，
                # 它们只是 /convert 的形参占位，中间层不消费，不再提交。
                **mix_kwargs,
                **msst_kwargs,
                **render_kwargs,
                **fade_kwargs,
                **reverb_kwargs,
            }
            result, cache_hit = await self._async_predict(
                client,
                **legacy_kwargs,
                api_name="/convert",
                timeout=self.inference_timeout,
                event=event,
                detect_cache_hit=True,
            )
            return result, cache_hit

    # ==================== 方案A+B：上下文注入 + 任务追踪辅助方法 ====================

    def _get_task_key(self, event: AstrMessageEvent) -> str:
        """生成任务追踪的唯一键（按会话维度）"""
        return str(event.unified_msg_origin)

    def _cleanup_expired_tasks(self):
        """清理超时的翻唱任务"""
        if not self.enable_task_tracking:
            return
        now = time.time()
        expired_keys = []
        for key, task_info in self._active_cover_tasks.items():
            start_time = task_info.get("start_time", 0)
            if now - start_time > self.task_timeout_seconds:
                expired_keys.append(key)
        for key in expired_keys:
            task_info = self._active_cover_tasks.pop(key, None) or {}
            job = task_info.get("gradio_job")
            task = task_info.get("asyncio_task")
            try:
                if job is not None:
                    job.cancel()
            except Exception as exc:
                logger.debug(f"清理超时 Gradio 任务失败: {exc}")
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
            logger.warning(f"[RVC控制台] 任务超时已清理: {key}")

    def _register_active_task(self, event: AstrMessageEvent, song_name: str, model_display: str = "") -> bool:
        """注册正在进行的翻唱任务（CUSTOM-8：同时登记进统一任务注册表）"""
        if not self.enable_task_tracking:
            return True
        self._cleanup_expired_tasks()
        key = self._get_task_key(event)
        # CUSTOM-8.1：移除「同会话单任务」限制——同会话新任务进入统一队列排队
        # （由防重复/全局等待上限/模块队列控制并发），不再直接拒绝。
        # CUSTOM-8：防重复提交（同用户+同会话+同请求，窗口内且仍在进行中）
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            uid = self._get_user_id(event)
            dup = self.tasks.find_duplicate(uid, umo, song_name)
            if dup is not None:
                self._last_register_reject_reason = (
                    f"⏳ 这首《{song_name}》已经在排队/处理了（任务 {dup.task_id[:8]}），"
                    "等结果就行，不用重复提交。")
                logger.info(f"[TaskRegistry] 重复提交被拦截: {song_name} (existing={dup.task_id})")
                return False
            rec = self.tasks.register(
                task_type="cover", conversation_id=umo, user_id=uid,
                user_name=self._sender_pair(event)[1],
                group_id=str(getattr(event, "get_group_id", lambda: "")() or ""),
                message_id=str(getattr(getattr(event, "message_obj", None), "message_id", "") or ""),
                request_text=song_name)
            if rec is None:
                self._last_register_reject_reason = (
                    f"⏳ 全局任务队列已满（上限 {self.tasks.max_pending}），请稍后再试。")
                return False
            self.tasks.set_status(rec.task_id, ST_QUEUED)
        except Exception as exc:  # noqa: BLE001 — 注册表故障不阻塞翻唱主流程
            logger.warning(f"[TaskRegistry] 登记失败（不影响翻唱）: {exc}")
            rec = None
        entry_key = f"{key}:{rec.task_id if rec else 'noreg'}"
        self._active_cover_tasks[entry_key] = {
            "song_name": song_name,
            "model_display": model_display,
            "status": "处理中",
            "start_time": time.time(),
            "asyncio_task": None,
            "gradio_job": None,
            "registry_id": rec.task_id if rec else "",
            "base_key": key,
        }
        # 记录到事件上：同会话并发任务各自绑定各自的记录
        try:
            setattr(event, "_cover_reg_key", entry_key)
            if rec:
                setattr(event, "_cover_registry_id", rec.task_id)
        except Exception:  # noqa: BLE001
            pass
        self._last_register_reject_reason = ""
        logger.info(f"[RVC控制台] 注册翻唱任务: {entry_key} -> 《{song_name}》"
                    + (f"（registry={rec.task_id}）" if rec else ""))
        return True
    def _reg_key(self, event: AstrMessageEvent) -> str:
        """本事件对应任务在 _active_cover_tasks 里的唯一键。"""
        return str(getattr(event, "_cover_reg_key", "") or "") or self._get_task_key(event)

    def _bind_active_asyncio_task(self, event: AstrMessageEvent, task: Optional[asyncio.Task] = None) -> None:
        """把真实 asyncio 任务绑定到任务记录，供取消命令使用。"""
        if not self.enable_task_tracking:
            return
        info = self._active_cover_tasks.get(self._reg_key(event))
        if info is not None:
            info["asyncio_task"] = task or asyncio.current_task()
        rec = self.tasks.get(str(getattr(event, "_cover_registry_id", "") or ""))
        if rec is not None:
            rec.asyncio_task = task or asyncio.current_task()

    def _bind_active_gradio_job(self, event: Optional[AstrMessageEvent], job: Any) -> None:
        if not self.enable_task_tracking or event is None:
            return
        info = self._active_cover_tasks.get(self._reg_key(event))
        if info is not None:
            info["gradio_job"] = job
        rec = self.tasks.get(str(getattr(event, "_cover_registry_id", "") or ""))
        if rec is not None:
            rec.gradio_job = job

    def _clear_active_gradio_job(self, event: Optional[AstrMessageEvent], job: Any) -> None:
        if not self.enable_task_tracking or event is None:
            return
        info = self._active_cover_tasks.get(self._reg_key(event))
        if info is not None and info.get("gradio_job") is job:
            info["gradio_job"] = None
        rec = self.tasks.get(str(getattr(event, "_cover_registry_id", "") or ""))
        if rec is not None and rec.gradio_job is job:
            rec.gradio_job = None

    def _finish_active_task(self, event: AstrMessageEvent, status: str = "已完成",
                            error: str = "", timeout: bool = False):
        """标记翻唱任务结束（CUSTOM-8：同步终结统一注册表里的记录）"""
        if not self.enable_task_tracking:
            return
        reg_key = self._reg_key(event)
        if reg_key in self._active_cover_tasks:
            info = self._active_cover_tasks.pop(reg_key)
            reg_id = info.get("registry_id")
            if reg_id:
                try:
                    if status == "已取消":
                        self.tasks.terminate_cancelled(reg_id)
                    else:
                        self.tasks.finish_by_id(reg_id, error=error, timeout=timeout)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[TaskRegistry] 终结记录失败: {exc}")
            logger.info(f"[RVC控制台] 翻唱任务结束: {reg_key} -> {status}")

    def _get_active_task_status(self, event: AstrMessageEvent) -> Optional[Dict]:
        """获取当前会话的活跃任务状态（同会话并发任务时返回最新的一个）"""
        if not self.enable_task_tracking:
            return None
        self._cleanup_expired_tasks()
        base = self._get_task_key(event)
        entries = [v for k, v in self._active_cover_tasks.items()
                   if k == base or k.startswith(base + ":")]
        if not entries:
            return None
        return max(entries, key=lambda v: v.get("start_time", 0))

    async def _inject_message_to_context(self, event: AstrMessageEvent, content: str, role: str = "assistant"):
        """将消息注入到 LLM 的对话上下文中（方案A）"""
        if not self.enable_inject_context:
            return
        try:
            conv_mgr = self.context.conversation_manager
            unified_msg_origin = event.unified_msg_origin
            cid = await conv_mgr.get_curr_conversation_id(unified_msg_origin)
            if not cid:
                logger.debug("[RVC控制台] 未找到当前对话ID，无法注入上下文")
                return
            conv = await conv_mgr.get_conversation(unified_msg_origin, cid)
            if not conv:
                logger.debug("[RVC控制台] 未找到对话对象，无法注入上下文")
                return
            import json
            history = json.loads(conv.history) if conv.history else []
            history.append({"role": role, "content": content})
            await conv_mgr.update_conversation(unified_msg_origin, cid, history=history)
            logger.debug(f"[RVC控制台] 已注入{role}消息到上下文: {content[:50]}...")
        except Exception as e:
            logger.error(f"[RVC控制台] 注入上下文失败: {e}")

    def _is_generic_persona(self, persona) -> bool:
        """判断是否是系统默认/通用助手人格。"""
        if not isinstance(persona, dict):
            return True
        prompt = str(persona.get("system_prompt", "") or "").strip().lower()
        if not prompt:
            return True
        return ("helpful" in prompt and len(prompt) < 120) or prompt.startswith("you are a helpful")

    def _find_persona(self, persona_id: str):
        """按 name / persona_id / id 三种键查找人格。"""
        pid = str(persona_id or "").strip()
        if not pid:
            return None
        manager = getattr(self.context, "persona_manager", None)
        items = getattr(manager, "personas_v3", None) or []
        for item in items:
            if str(item.get("name") or "") == pid:
                return item
        for item in items:
            if str(item.get("persona_id") or "") == pid or str(item.get("id") or "") == pid:
                return item
        return None

    async def _notify_llm_with_context(self, event: AstrMessageEvent, song_name: str, status_type: str, result_detail: str = ""):
        """通知 LLM 翻唱结果，并将通知内容注入上下文（方案A+B整合）"""
        if not self.enable_llm_success_notify:
            return None
        
        try:
            umo = event.unified_msg_origin
            provider = self.context.get_using_provider(umo)
            if not provider:
                logger.debug("[RVC控制台] 未找到 provider，无法通知 LLM")
                return None

            # CUSTOM-5.1：取当前会话人格 + 最近上下文，否则模型只会给通用腔
            system_prompt = ""
            contexts = []
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                conversation = (
                    await self.context.conversation_manager.get_conversation(umo, cid)
                    if cid else None
                )
                persona_id = getattr(conversation, "persona_id", None)
                try:
                    platform_name = event.get_platform_name() or ""
                except Exception:  # noqa: BLE001
                    platform_name = ""
                chosen = self._find_persona(getattr(self, "notify_persona_id", ""))
                if chosen is None:
                    try:
                        _pid, persona, _force, _web = await self.context.persona_manager.resolve_selected_persona(
                            umo=umo,
                            conversation_persona_id=persona_id,
                            platform_name=platform_name,
                        )
                        chosen = persona
                    except Exception:  # noqa: BLE001
                        chosen = None
                    # 解析到的是通用助手人格时，退而求其次用第一个自定义人格（聊天里就是这个）
                    if self._is_generic_persona(chosen):
                        alt = next(
                            (p for p in (getattr(self.context.persona_manager, "personas_v3", None) or [])
                             if not self._is_generic_persona(p)),
                            None)
                        if alt is not None:
                            chosen = alt
                if chosen:
                    system_prompt = str(chosen.get("system_prompt", "") or "")
                    logger.info(
                        "[RVC控制台] 成功播报使用人格: %s（提示词 %d 字，来源=%s）",
                        chosen.get("name") or chosen.get("persona_id") or "-",
                        len(system_prompt),
                        "配置指定" if getattr(self, "notify_persona_id", "") else "会话自动解析")
                history = getattr(conversation, "message", None) if conversation else None
                if isinstance(history, list) and history:
                    contexts = history[-6:]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RVC控制台] 取人格/上下文失败，退回普通调用: %s", exc)
            
            if status_type == "failed":
                fact = (
                    f"《{song_name}》翻唱失败，原因：{result_detail}。"
                    "请用你的人设简短说一句（可建议重试或换一首），不要解释技术细节。")
            elif status_type == "cache_hit":
                fact = (
                    f"《{song_name}》之前唱过，命中了已有成品，音频已经发出。"
                    "请用你的人设简短回应一句。")
            else:  # success
                fact = (
                    f"《{song_name}》翻唱完成，音频已经发出。"
                    "请用你的人设简短回应一句。")
            
            prompt = (
                "这是后台状态通知，绝对不要复述“系统通知/后台/任务/生成成功”这类词。\n"
                f"{fact}\n"
                "要求：完全用你自己在这个群里的说话方式，一到两句，尽量不超过 20 字；"
                "保持惯用的省略号和低情绪表达，少用感叹号；"
                "不要用“好消息/耶/啦/咯/快去听听/快去听吧”这类客服话术。"
            )
            llm_resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts or None,
                system_prompt=system_prompt or None,
            )
            reply_text = llm_resp.completion_text if llm_resp else ""
            
            if reply_text:
                await event.send(event.plain_result(reply_text))
                # 方案A：将通知内容注入 LLM 上下文
                await self._inject_message_to_context(event, reply_text, role="assistant")
            
            return reply_text
        except Exception as e:
            logger.error(f"[RVC控制台] 通知 LLM 失败: {e}")
            return None

    def _set_engine_models(self, models: list) -> None:
        """把刷新拿到的模型列表（含别名）写回内存和插件配置。"""
        self.rvc_models_keywords = models
        self.config["rvc_models_keywords"] = models

    def get_models_display_list(self):
        models_keywords = self.rvc_models_keywords
        display_names, key_list = [], []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            display_name = " ".join((alias or os.path.splitext(model_name)[0]).split())
            display_names.append(f"{index}. {display_name}")
            key_list.append(model_name)
        return "\n".join(display_names), key_list

    @staticmethod
    def _format_cache_bytes(value: Any) -> str:
        try:
            size = float(value)
        except (TypeError, ValueError):
            return "未知"
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if size < 1024 or unit == "TiB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TiB"

    async def _backend_cache_request(self, clear: bool = False) -> Any:
        async with self._get_gradio_client(self._gateway_base_url()) as client:
            if clear:
                return await self._async_predict(
                    client, scope="all", api_name="/clear_cache", timeout=60
                )
            return await self._async_predict(
                client, api_name="/cache_info", timeout=30
            )

    @staticmethod
    def _parse_cache_target(message: str) -> list[str]:
        """缓存命令只认 RVC 一个后端；传别的词返回空列表，让调用方提示用法。"""
        normalized = str(message or "").strip().lower()
        for command in ("查看翻唱缓存", "清理翻唱缓存"):
            normalized = re.sub(rf"^/?{command}\s*", "", normalized).strip()
        if normalized in ("", "rvc", "all", "全部"):
            return ["rvc"]
        return []

    def get_models_detailed_list(self):
        """获取包含完整信息的模型列表（用于LLM匹配）"""
        models_keywords = self.rvc_models_keywords
        detailed_info = []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            name_without_ext = os.path.splitext(model_name)[0]
            detailed_info.append({
                "index": index,
                "filename": model_name,
                "name": name_without_ext,
                "alias": alias,
                "display": alias or name_without_ext
            })
        return detailed_info

    def _find_model_index_by_name(self, model_name_input: str) -> Optional[int]:
        """根据模型名称或别名查找模型序号（支持模糊匹配）"""
        if not model_name_input:
            return None

        models = self.get_models_detailed_list()
        input_lower = model_name_input.lower().strip()

        exact_matches = []
        partial_matches = []

        for model in models:
            filename_lower = model["filename"].lower()
            name_lower = model["name"].lower()
            alias_lower = model["alias"].lower()

            if input_lower == filename_lower or input_lower == name_lower or (alias_lower and input_lower == alias_lower):
                exact_matches.append(model)
            elif input_lower in filename_lower or input_lower in name_lower or (alias_lower and input_lower in alias_lower):
                partial_matches.append(model)

        if exact_matches:
            return exact_matches[0]["index"]
        elif partial_matches:
            best_match = max(partial_matches, key=lambda m: (
                len(m["name"]) if input_lower in m["name"] else 0,
                len(m["alias"]) if m["alias"] and input_lower in m["alias"] else 0
            ))
            return best_match["index"]

        return None

    def _configured_rvc_model_index(self) -> Optional[int]:
        """把配置里下拉选的 rvc_default_model 换成当前 RVC 模型列表里的序号。

        只在调用方确认「用户没有显式指定模型名或序号」之后调用。
        下拉选的模型不在当前列表里（还没 /刷新rvc模型、模型被删了）时返回 None，
        调用方回落到第 1 个模型 / 偏好序号；这里只记一条 warning。
        """
        # CUSTOM-9：实时读配置——WebUI 改默认音色即生效（此前缓存启动时的值，
        # 设置里换了音色、正在跑/新提交的任务仍用旧模型）
        configured = str(self.config.get("rvc_default_model", "") or "").strip()
        if not configured:
            return None
        target = configured.lower()
        for model in self.get_models_detailed_list():
            if target in (model["filename"].lower(), model["name"].lower(), model["alias"].lower()):
                return model["index"]
        logger.warning(
            f"rvc_default_model={configured} 不在当前 RVC 模型列表里，"
            f"回落到第 {FALLBACK_MODEL_INDEX} 个模型；"
            "可以用 /刷新rvc模型 重新获取模型列表"
        )
        return None

    # 版本/改编类关键词：命中即降权（说明大概率不是原唱正式版）
    _NON_ORIGINAL_MARKERS = (
        "翻唱", "cover", "女声", "男声", "童声", "钢琴", "吉他", "小提琴", "二胡", "古筝", "萨克斯",
        "dj", "remix", "混音", "电音", "纯音乐", "伴奏", "清唱", "慢版", "加快", "变速", "变调",
        "空灵", "深情", "温柔", "治愈", "抒情", "戏腔", "和声", "合唱", "改编", "串烧", "剪辑",
        "片段", "高潮", "副歌", "试听", "demo", "饭拍", "现场", "live", "演唱会", "重制", "重置",
        "英文版", "日文版", "韩文版", "粤语版", "方言版", "翻奏", "指弹", "弹唱",
    )
    _ORIGINAL_HINT_MARKERS = ("原唱", "原版", "正式版", "专辑版", "原声")

    # 常见歌手中英文名别名（音源平台歌手字段可能显示英文名，如网易云把周杰伦写作 Jay）。
    # 只收录大众熟知、无歧义的映射；没收录的歌手仍按字面匹配。
    _ARTIST_ALIASES = {
        "周杰伦": ("jay chou", "jay", "周杰倫"),
        "周杰倫": ("jay chou", "jay", "周杰伦"),
        "邓紫棋": ("g.e.m.",),
        "林俊杰": ("jj lin",),
        "薛之谦": ("joker xue",),
        "泰勒斯威夫特": ("taylor swift",),
        "艾德希兰": ("ed sheeran",),
        "碧梨": ("billie eilish",),
    }

    @staticmethod
    def _claimed_original_name(text: str) -> str:
        """从「(原唱 XXX)」「[原唱: XXX]」里抠出标注的原唱名；抠不出返回空。"""
        import re as _re_claimed
        m = _re_claimed.search(r"(?:原唱|原版|正式版|专辑版)\s*[:：]?\s*([^\)\]】»|）]{2,15})", text or "")
        if not m:
            return ""
        return m.group(1).strip().casefold()

    async def _auto_pick_from_sources(
        self, song_name: str, artist_hint: str = "", flow_id: str = ""
    ) -> dict:
        """公共自动选歌：逐音源搜索 + 逐音源评分，第一个可信的源直接选中。

        `/rvc` 命令与 smart_cover 自然语言入口都走这里，保证行为一致。
        Returns:
            dict(selected=歌曲dict|None, songs=菜单兜底候选, source_display=str,
                 picked_reason=str, pick_score=float, all_accompaniment=bool)
        """
        # 带歌手提示时把歌手拼进搜索词（如「周杰伦 晴天」）：
        # 网易云对裸歌名的候选常全是翻唱，带歌手才能搜到真原唱；评分仍按 song_name+hint。
        search_kw = f"{artist_hint} {song_name}".strip() if artist_hint else song_name
        per_source = await self.song_router.search_candidates_by_source(
            search_kw, limit=10
        )
        cleaned = []
        for source, cands in per_source:
            ok = [
                c for c in cands
                if not source_router.is_accompaniment(c.name, c.alias)
            ]
            if ok:
                cleaned.append((source, ok))
        if not cleaned:
            logger.info(
                "[自动选歌] 各音源候选全是伴奏版或无结果: %s（flow=%s）",
                song_name, flow_id or "-")
            return {"selected": None, "songs": [], "source_display": "",
                    "picked_reason": "", "pick_score": 0.0,
                    "all_accompaniment": True}
        first_source, first_playable = cleaned[0]
        songs = [c.as_dict() for c in first_playable]
        source_display = source_router.SOURCE_DISPLAY.get(first_source, first_source)
        selected, reason, score = None, "", 0.0
        for source, playable in cleaned:
            src_songs = [c.as_dict() for c in playable]
            picked, s, r = self._auto_pick_song(
                song_name, src_songs, artist_hint=artist_hint)
            if picked is not None:
                selected, score, reason = src_songs[picked], s, r
                songs = src_songs
                source_display = source_router.SOURCE_DISPLAY.get(source, source)
                logger.info(
                    "[自动选歌] %s -> %s - %s（评分 %.1f，原因：%s，来源：%s，flow=%s）",
                    song_name, selected["name"], selected["artists"],
                    s, r, source, flow_id or "-")
                break
            logger.info(
                "[自动选歌] 音源 %s 区分度不足（最高 %.1f），尝试下一音源（flow=%s）",
                source, s, flow_id or "-")
        if selected is None:
            logger.info(
                "[自动选歌] 各音源均区分度不足，回退菜单/严格匹配（flow=%s）",
                flow_id or "-")
        return {"selected": selected, "songs": songs,
                "source_display": source_display, "picked_reason": reason,
                "pick_score": score, "all_accompaniment": False}

    def _auto_pick_song(self, query: str, songs: list, artist_hint: str = ""):
        """从候选里挑最像「原唱正式版」的一首。

        ⚠️ 这只是启发式打分（歌名/歌手/版本描述/来源排序），不保证选中的就是真原唱。
        分数不够或与次名差距太小时返回 (None, ...)，由调用方退回选择菜单，不硬猜。
        """
        if not songs:
            return None, 0.0, "没有候选"
        import difflib
        import re as _re

        q = str(query or "").strip().casefold()
        q_compact = _re.sub(r"\s+", "", q)
        _q_all = f"{q} {str(artist_hint or '').strip().casefold()}".strip()
        # 用户查询里明确点名的版本标记（如 女声版/DJ版）：命中这些标记的候选不降权
        q_markers = {m for m in self._NON_ORIGINAL_MARKERS if m in _q_all}
        # 预收集各候选的歌手 token，供「标题蹭他人原唱」检查用
        artist_token_list = []
        for song in songs:
            toks = [t for t in _re.split(r"[\s,，、/]+", str(song.get("artists") or ""))
                    if len(t) >= 2]
            artist_token_list.append(toks)
        scored = []
        for idx, song in enumerate(songs):
            name = str(song.get("name") or "")
            artists = str(song.get("artists") or "")
            alias = str(song.get("alias") or "")
            hay = f"{name} {artists} {alias}".casefold()
            hay_compact = _re.sub(r"\s+", "", hay)
            score = 0.0
            reasons = []
            # 1) 标题相似度
            score += difflib.SequenceMatcher(None, q, name.casefold()).ratio() * 50
            if q_compact and q_compact in hay_compact:
                score += 25
                reasons.append("标题/歌手命中查询词")
            # 2) 歌手匹配（含常见中英别名，如 周杰伦↔Jay Chou）
            if artists:
                matched_alias = None
                for token in _re.split(r"[\s,，、]+", _q_all):
                    if len(token) < 2:
                        continue
                    names = (token,) + self._ARTIST_ALIASES.get(token, ())
                    if any(n in artists.casefold() for n in names):
                        matched_alias = token
                        break
                if matched_alias:
                    score += 30
                    reasons.append(f"歌手命中「{matched_alias}」")
            # 3) 版本标记：查询明确指定的版本不降权（尊重用户指定，不强制原唱）
            hit_markers = [m for m in self._NON_ORIGINAL_MARKERS if m in hay]
            if hit_markers and not set(hit_markers) <= q_markers:
                score -= 25
                reasons.append("疑似翻唱/改编/器乐版，降权")
            elif hit_markers:
                score += 10
                reasons.append(f"用户指定版本「{'/'.join(hit_markers[:3])}」")
            if any(m in hay_compact for m in self._ORIGINAL_HINT_MARKERS):
                score += 20
                reasons.append("标注原唱/原版")
            # 3b) 标题里「原唱 XXX」标注的人和自己歌手对不上 → 大概率翻唱冒充
            claimed = self._claimed_original_name(name + " " + alias)
            if claimed and claimed not in artists.casefold():
                score -= 20
                reasons.append(f"标题标注原唱「{claimed}」与歌手不符，疑似翻唱")
            # 3c) 标题/别名蹭了其他候选的歌手名（自家歌手没有）→ 降权
            bumped = False
            for j, toks in enumerate(artist_token_list):
                if j == idx:
                    continue
                for token in toks:
                    if token in (name + alias).casefold() and token not in artists.casefold():
                        score -= 25
                        reasons.append(f"标题标注他人原唱「{token}」")
                        bumped = True
                        break
                if bumped:
                    break
            # 4) 来源顺序（source_order 已排好序）靠前略微加分
            score += max(0.0, 8.0 - float(idx))
            scored.append((score, idx, "；".join(reasons) or "仅按标题相似度"))

        scored.sort(key=lambda item: (-item[0], item[1]))
        ranked = " | ".join(
            "#%d %s(%s)=%.1f" % (i + 1, songs[item[1]].get("name", "?"),
                                 songs[item[1]].get("artists", "?"), item[0])
            for i, item in enumerate(scored[:3])
        )
        logger.info("[自动选歌] 候选排名（前3）：%s", ranked)
        best_score, best_idx, best_reason = scored[0]
        runner = scored[1][0] if len(scored) > 1 else 0.0
        margin = best_score - runner
        if best_score < self.auto_pick_min_score or margin < self.auto_pick_min_margin:
            logger.info(
                "[自动选歌] 判断不了：最高 %.1f（阈值 %.1f）、与次名差 %.1f（阈值 %.1f），保留菜单",
                best_score, self.auto_pick_min_score, margin, self.auto_pick_min_margin)
            return None, best_score, "候选区分度不足，保留菜单"
        return best_idx, best_score, best_reason

    def _load_model_genders(self, config) -> Dict[str, str]:
        """读取模型性别（唯一来源 = model_genders），并完成旧配置的一次性迁移。

        合并优先级：旧列表 < model_genders_extra < model_genders；unknown 视为未设置。
        迁移结果写回 model_genders/extra 并清空旧列表，可重复执行、不覆盖已有新值。
        """
        raw_obj = dict(config.get("model_genders", {}) or {})
        raw_extra = dict(config.get("model_genders_extra", {}) or {})
        legacy = self._parse_gender_map(config.get("model_gender_map", []) or [])
        merged: Dict[str, str] = {}
        for bucket in (legacy, raw_extra, raw_obj):
            for k, v in (bucket or {}).items():
                key = str(k).strip().lower()
                if key.endswith(".pth"):
                    key = key[:-4]
                g = self._norm_gender(v)
                if key and g in ("male", "female"):
                    merged[key] = g
        changed = False
        new_obj = dict(raw_obj)
        new_extra = dict(raw_extra)
        for key in SCHEMA_GENDER_MODEL_KEYS:
            if key not in new_obj:
                new_obj[key] = "未知"
                changed = True
        for key, g in merged.items():
            label = "男声" if g == "male" else "女声"
            bucket = new_obj if key in SCHEMA_GENDER_MODEL_KEYS else new_extra
            if self._norm_gender(bucket.get(key)) != g:
                bucket[key] = label
                changed = True
        if legacy:
            config["model_gender_map"] = []
            changed = True
        if changed:
            config["model_genders"] = new_obj
            config["model_genders_extra"] = new_extra
            try:
                config.save_config()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[配置迁移] 保存模型性别失败（本次运行仍生效）: %s", exc)
            logger.info("[配置迁移] 模型性别统一到 model_genders: %s", merged)
        self.model_genders = new_obj
        self.model_genders_extra = new_extra
        return merged

    def _write_model_gender(self, name: str, gender: str) -> None:
        """命令入口写性别：schema 内模型写 model_genders，新模型写 extra（不会被完整性检查删掉）。"""
        key = str(name).strip().lower().removesuffix(".pth")
        label = {"male": "男声", "female": "女声"}.get(gender, "未知")
        obj = dict(self.config.get("model_genders", {}) or {})
        extra = dict(self.config.get("model_genders_extra", {}) or {})
        if key in SCHEMA_GENDER_MODEL_KEYS:
            obj[key] = label
        else:
            extra[key] = label
        self.config["model_genders"] = obj
        self.config["model_genders_extra"] = extra
        legacy = [
            e for e in (self.config.get("model_gender_map", []) or [])
            if not (isinstance(e, str)
                    and e.split(":", 1)[0].strip().lower().removesuffix(".pth") == key)
        ]
        self.config["model_gender_map"] = legacy
        self.model_genders = obj
        self.model_genders_extra = extra
        merged = dict(self.model_gender_map)
        if gender in ("male", "female"):
            merged[key] = gender
        else:
            merged.pop(key, None)
        self.model_gender_map = merged
        self.config.save_config()

    @staticmethod
    def _merge_model_genders(obj: dict, legacy: Dict[str, str]) -> Dict[str, str]:
        """model_genders（WebUI 下拉，object）为主，legacy 列表补缺；unknown 视为未设置。"""
        merged = dict(legacy or {})
        for k, v in (obj or {}).items():
            key = str(k).strip().lower().removesuffix(".pth")
            if v in ("male", "female"):
                merged[key] = v
        return merged

    @staticmethod
    def _norm_gender(raw: str) -> Optional[str]:
        t = str(raw or "").strip().lower()
        if t in ("男", "男声", "male", "m"):
            return "male"
        if t in ("女", "女声", "female", "f"):
            return "female"
        if t in ("未知", "unknown", "none", "清除", "取消"):
            return "unknown"
        return None

    @staticmethod
    def _gender_label(g) -> str:
        return {"male": "男声", "female": "女声"}.get(g, "未知")

    @staticmethod
    def _default_model_note(configured: str, new_models_list: list) -> str:
        """刷新后检查默认音色是否仍在新列表；只给提示，绝不清空配置。"""
        configured = str(configured or "").strip()
        if not configured:
            return ""
        bases = set()
        for item in new_models_list:
            name = str(item).split(MODEL_ALIAS_SEPARATOR, 1)[0]
            bases.add(name.lower())
            bases.add(os.path.splitext(name)[0].lower())
        if configured.lower() in bases or configured.lower().removesuffix(".pth") in bases:
            return ""
        return configured

    def _parse_gender_map(self, entries: list) -> Dict[str, str]:
        result = {}
        for entry in entries:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            parts = entry.split(":", 1)
            name = parts[0].strip()
            gender = parts[1].strip().lower()
            if name and gender in ("male", "female"):
                result[name] = gender
        return result

    def _add_to_gender_cache(self, cache_key: str, value: str):
        """安全地写入性别缓存，防止无限增长"""
        if len(self._gender_cache) >= self._gender_cache_max_size:
            keys_to_remove = list(self._gender_cache.keys())[:self._gender_cache_max_size // 2]
            for k in keys_to_remove:
                del self._gender_cache[k]
        self._gender_cache[cache_key] = value

    def _detect_artist_gender(self, artist_name: str) -> Optional[str]:
        if not artist_name:
            return None
        artist_lower = artist_name.strip().lower()
        for mapped_name, gender in self.artist_gender_map.items():
            if mapped_name.strip().lower() == artist_lower:
                return gender
        return None

    async def _detect_artist_gender_llm(self, artist_name: str, event: AstrMessageEvent = None) -> Optional[str]:
        if not artist_name:
            return None
        cache_key = f"artist:{artist_name.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：歌手「{artist_name}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        mapped = self._detect_artist_gender(artist_name)
        if mapped:
            self._add_to_gender_cache(cache_key, mapped)
            logger.info(f"自动升降调：歌手「{artist_name}」在映射表中找到，性别={mapped}")
            return mapped
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider（event={'有' if event else '无'}），跳过歌手性别判断")
                return None
            prompt = (
                f"请判断歌手或组合「{artist_name}」的主唱性别。"
                f"如果是组合，请判断主唱（唱主要部分的人）的性别。"
                f"只回答'男'或'女'，不要回答其他任何内容。"
                f"如果不确定，请优先根据该名字在日语/华语/欧美流行乐坛的常见用法进行推断，"
                f"不要仅凭字面含义判断。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断歌手「{artist_name}」性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
                else:
                    prompt2 = (
                        f"「{artist_name}」的主唱是男性还是女性？"
                        f"这是一位歌手或音乐组合，请结合流行音乐常识判断。"
                        f"只回答'男'或'女'。"
                    )
                    resp2 = await provider.text_chat(prompt=prompt2)
                    if resp2 and resp2.completion_text:
                        answer2 = resp2.completion_text.strip()
                        logger.info(f"自动升降调：LLM二次判断歌手「{artist_name}」性别 → {answer2}")
                        if "女" in answer2:
                            self._add_to_gender_cache(cache_key, "female")
                            return "female"
                        elif "男" in answer2:
                            self._add_to_gender_cache(cache_key, "male")
                            return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断歌手「{artist_name}」性别")
        except Exception as e:
            logger.error(f"LLM 判断歌手性别失败: {e}")
        return None

    def _detect_model_gender(self, model_display: str) -> Optional[str]:
        if not model_display:
            return None
        model_lower = model_display.strip().lower()
        if model_lower.endswith(".pth"):
            model_lower = model_lower[:-4]
        for mapped_name, gender in self.model_gender_map.items():
            mapped_lower = mapped_name.strip().lower()
            if mapped_lower.endswith(".pth"):
                mapped_lower = mapped_lower[:-4]
            if mapped_lower == model_lower:
                logger.info(
                    f"自动升降调：模型「{model_display}」命中性别映射表 → {gender}（来源=配置）")
                return gender
        logger.info(
            f"自动升降调：模型「{model_display}」不在性别映射表中，转由 LLM 判断"
            "（在插件配置「模型性别映射表」加一行如 deng:male 可跳过 LLM）")
        return None

    async def _detect_model_gender_llm(self, model_display: str, event: AstrMessageEvent = None) -> Optional[str]:
        if not model_display:
            return None
        cache_key = f"model:{model_display.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：模型「{model_display}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        mapped = self._detect_model_gender(model_display)
        if mapped:
            self._add_to_gender_cache(cache_key, mapped)
            logger.info(f"自动升降调：模型「{model_display}」在映射表中找到，性别={mapped}")
            return mapped
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider，跳过模型性别判断")
                return None
            prompt = (
                f"AI语音模型「{model_display}」模拟的角色是男性还是女性？"
                f"只回答'男'或'女'。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断模型「{model_display}」性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断模型「{model_display}」性别")
        except Exception as e:
            logger.error(f"LLM 判断模型性别失败: {e}")
        return None

    async def _detect_gender_pair(
        self,
        source_name: str,
        model_display: str,
        event: AstrMessageEvent = None,
        source_is_song: bool = False,
    ) -> tuple[Optional[str], Optional[str]]:
        """并发识别来源与模型性别；LLM 迟迟不返回时快速回退。"""
        source_label = "歌曲" if source_is_song else "歌手"

        async def _with_timeout(coro, label: str) -> Optional[str]:
            try:
                return await asyncio.wait_for(
                    coro, timeout=self.gender_detection_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[自动升降调Debug] {label}识别超过 "
                    f"{self.gender_detection_timeout} 秒，已跳过并继续翻唱"
                )
                return None
            except asyncio.CancelledError:
                # CUSTOM-6.2：Provider 内部超时会抛 CancelledError（不是 TimeoutError），
                # 之前会直接取消整个后台翻唱任务，用户什么也收不到。
                cur = asyncio.current_task()
                pending = 0
                try:
                    pending = int(getattr(cur, "cancelling", lambda: 0)() or 0)
                except Exception:
                    pending = 0
                if pending > 0:
                    raise  # 真正的外部取消（重载/取消命令），照常向上传播
                logger.warning(
                    f"[自动升降调Debug] {label}识别被上游中断（LLM 请求取消），"
                    "按未知处理并继续翻唱"
                )
                return None
            except Exception as exc:
                logger.warning(f"[自动升降调Debug] {label}识别失败，已回退: {exc}")
                return None

        source_coro = (
            self._detect_song_gender_llm(source_name, event)
            if source_is_song
            else self._detect_artist_gender_llm(source_name, event)
        )
        logger.info(
            f"[自动升降调Debug] 开始并发识别{source_label}与模型性别 | "
            f"timeout={self.gender_detection_timeout}s | "
            f"source={source_name} | model={model_display}"
        )
        source_gender, model_gender = await asyncio.gather(
            _with_timeout(source_coro, source_label),
            _with_timeout(
                self._detect_model_gender_llm(model_display, event), "模型"
            ),
        )
        logger.info(
            f"[自动升降调Debug] 性别识别结束 | {source_label}={source_gender} | "
            f"模型={model_gender}"
        )
        return source_gender, model_gender

    async def _detect_song_gender_llm(self, song_name: str, event: AstrMessageEvent = None) -> Optional[str]:
        """根据歌曲名判断原唱歌手性别（用于本地音频翻唱的自动升降调）"""
        if not song_name:
            return None
        cache_key = f"song:{song_name.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：歌曲「{song_name}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        
        # Step 1: 先尝试从 artist_gender_map 中匹配歌曲名中的歌手名
        song_lower = song_name.lower()
        for artist, gender in self.artist_gender_map.items():
            if artist.lower() in song_lower:
                logger.info(f"自动升降调：歌曲「{song_name}」匹配歌手映射表 '{artist}' → {gender}")
                self._add_to_gender_cache(cache_key, gender)
                return gender
        
        # Step 2: 本地关键词快速推断（常见女声/男声歌曲标记）
        female_song_markers = ["believers", "so'fly", "miku", "初音", "洛天依", "宅舞", " anime", "动漫", "acg", "女声", "女版", "翻唱版(女)", "cover by 女", "version by 女"]
        male_song_markers = ["男声", "男版", "cover by 男", "version by 男"]
        if any(m in song_lower for m in female_song_markers):
            logger.info(f"自动升降调：歌曲「{song_name}」匹配女声关键词 → female")
            self._add_to_gender_cache(cache_key, "female")
            return "female"
        if any(m in song_lower for m in male_song_markers):
            logger.info(f"自动升降调：歌曲「{song_name}」匹配男声关键词 → male")
            self._add_to_gender_cache(cache_key, "male")
            return "male"
        
        # Step 3: LLM 判断（兜底）
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider，跳过歌曲性别判断")
                return None
            prompt = (
                f"请判断歌曲「{song_name}」的原唱歌手性别。"
                f"只回答'男'或'女'，不要回答其他任何内容。"
                f"如果是日语/动漫/JPOP歌曲，通常女声演唱者较多；如果是华语流行摇滚，男性演唱者较多。"
                f"如果不确定，请优先根据该歌曲在JPOP/动漫/华语流行乐坛的常见演唱者进行推断。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断歌曲「{song_name}」原唱性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
                else:
                    prompt2 = (
                        f"歌曲「{song_name}」的原唱是男性还是女性？"
                        f"这是一首JPOP/动漫/流行歌曲，请结合音乐常识判断。只回答'男'或'女'。"
                    )
                    resp2 = await provider.text_chat(prompt=prompt2)
                    if resp2 and resp2.completion_text:
                        answer2 = resp2.completion_text.strip()
                        logger.info(f"自动升降调：LLM二次判断歌曲「{song_name}」原唱性别 → {answer2}")
                        if "女" in answer2:
                            self._add_to_gender_cache(cache_key, "female")
                            return "female"
                        elif "男" in answer2:
                            self._add_to_gender_cache(cache_key, "male")
                            return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断歌曲「{song_name}」原唱性别")
        except Exception as e:
            logger.error(f"LLM 判断歌曲原唱性别失败: {e}")
        return None

    async def _calc_auto_key_shift(self, artist_name: str, model_display: str, user_key_shift: int, artist_gender: Optional[str] = None, model_gender: Optional[str] = None, event: AstrMessageEvent = None, detection_attempted: bool = False) -> int:
        if not self.enable_auto_key_shift:
            return user_key_shift
        if user_key_shift != 0:
            return user_key_shift

        if not artist_gender and not detection_attempted:
            artist_gender = await self._detect_artist_gender_llm(artist_name, event)
        if not model_gender and not detection_attempted:
            model_gender = await self._detect_model_gender_llm(model_display, event)

        gender_label = {"male": "男", "female": "女", None: "未知"}
        final_shift = user_key_shift
        debug_reason = ""

        if not artist_gender or not model_gender:
            logger.info(
                "自动升降调：无法确定性别（歌手=%s, 模型=%s），跳过。"
                "在插件配置「模型性别映射表」加一行（如 deng:male）可跳过 LLM 直接生效",
                artist_gender, model_gender)
            debug_reason = "无法确定性别，跳过自动升降调"
        elif artist_gender == model_gender:
            logger.info(f"自动升降调：歌手和模型性别相同（{artist_gender}），无需调整")
            debug_reason = f"性别相同({gender_label.get(artist_gender, '?')})，无需调整"
        elif model_gender == "female" and artist_gender == "male":
            final_shift = self.male_to_female_shift
            logger.info(f"自动升降调：男→女，升调 {final_shift}")
            debug_reason = f"男歌手 → 女模型，自动升调 {final_shift:+d}"
        elif model_gender == "male" and artist_gender == "female":
            final_shift = self.female_to_male_shift
            logger.info(f"自动升降调：女→男，降调 {final_shift}")
            debug_reason = f"女歌手 → 男模型，自动降调 {final_shift:+d}"
        else:
            debug_reason = "未匹配到自动升降调规则"

        if self.enable_auto_key_shift_debug and event:
            try:
                debug_msg = (
                    f"🎵 [自动升降调Debug]\n"
                    f"歌手：{artist_name} → {gender_label.get(artist_gender, '未知')}\n"
                    f"模型：{model_display} → {gender_label.get(model_gender, '未知')}\n"
                    f"结果：{debug_reason}"
                )
                await event.send(event.plain_result(debug_msg))
            except Exception:
                pass

        return final_shift

    async def _fetch_models_info(self):
        """拉一次 /model_info，返回 (info, models)。

        中间层 0.9.0 才有 ``/model_info``（带目录、数量、索引目录）；老版本没有就退回
        ``/show_model``，功能不受影响，只是拿不到目录信息。
        """
        info = {}
        models = None
        async with self._get_gradio_client(self._gateway_base_url()) as client:
            if self._client_supports_api(client, "/model_info") is not False:
                try:
                    info = await self._async_predict(
                        client,
                        models_dir=self.rvc_models_dir,
                        api_name="/model_info",
                        timeout=60,
                    )
                except Exception as e:
                    logger.warning(f"读取模型目录失败，改用 /show_model: {e}")
                    info = {}

            if isinstance(info, dict) and isinstance(info.get("models"), list):
                models = info["models"]
            else:
                try:
                    models = await self._async_predict(client, api_name="/show_model")
                except Exception as e:
                    logger.warning(f"第一次获取模型列表失败，重试中: {e}")
                    models = await self._async_predict(client, api_name="/show_model")

        if not isinstance(models, list):
            raise ValueError(f"获取模型列表失败: {models}")

        if isinstance(info, dict) and info:
            self._models_dir_info = info
        return (info if isinstance(info, dict) else {}), models

    async def _update_models_from_api(self):
        """从 RVC 后端拉模型列表；失败或空列表时一律保留现有配置，绝不清空。"""
        try:
            info, model_list_from_api = await self._fetch_models_info()
        except Exception as exc:  # noqa: BLE001
            logger.error("刷新 RVC 模型失败（保留原配置）: %s", exc)
            return {"ok": False, "reason": f"后端不可用：{exc}", "missing_default": ""}
        model_list_from_api = [
            str(m).strip() for m in (model_list_from_api or []) if str(m).strip()
        ]
        if not model_list_from_api:
            logger.warning("刷新 RVC 模型返回空列表，保留原有模型列表与所有已选值")
            return {"ok": False,
                    "reason": "后端返回空列表（模型目录可能暂时不可读），已保留原配置",
                    "missing_default": ""}

        if info.get("dir"):
            logger.info(
                "RVC 模型目录：%s（%d 个 .pth，来源：%s）",
                info.get("dir"),
                int(info.get("count") or 0),
                info.get("source") or "中间层",
            )
            if info.get("warning"):
                logger.warning("RVC 模型目录有疑点：%s", info["warning"])

        old_aliases = {}
        for item_str in self.rvc_models_keywords:
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            if len(parts) > 1:
                old_aliases[parts[0]] = parts[1]

        new_models_list = [
            f"{m}{MODEL_ALIAS_SEPARATOR}{old_aliases.get(m, '') or ''}"
            for m in model_list_from_api
        ]

        # 刷新只更新模型列表，绝不动 rvc_default_model / 性别映射等已有选择
        self._set_engine_models(new_models_list)

        self.config.save_config()
        logger.info(f"RVC 模型列表已更新并成功保存，共 {len(new_models_list)} 个")
        # 只更新模型列表：默认音色、别名、性别映射一律不动；不在新列表时只提示不改值
        return {"ok": True, "reason": "",
                "missing_default": self._default_model_note(
                    str(self.config.get("rvc_default_model", "") or ""),
                    new_models_list)}

    @staticmethod
    def _client_supports_api(client, api_name: str) -> Optional[bool]:
        """Inspect the fetched Gradio config without issuing an extra request."""
        endpoints = getattr(client, "endpoints", None)
        if not endpoints:
            return None
        values = endpoints.values() if isinstance(endpoints, dict) else endpoints
        names = []
        for endpoint in values:
            name = getattr(endpoint, "api_name", None)
            if name:
                names.append("/" + str(name).lstrip("/"))
        if not names:
            return None
        return "/" + api_name.lstrip("/") in names

    @staticmethod
    def _client_api_parameter_names(client, api_name: str) -> Optional[Set[str]]:
        """Return keyword parameter names advertised for one Gradio endpoint."""
        endpoints = getattr(client, "endpoints", None)
        if not endpoints:
            return None
        values = endpoints.values() if isinstance(endpoints, dict) else endpoints
        expected = "/" + api_name.lstrip("/")
        for endpoint in values:
            name = getattr(endpoint, "api_name", None)
            if not name or "/" + str(name).lstrip("/") != expected:
                continue
            parameters = getattr(endpoint, "parameters_info", None)
            if parameters is None:
                return None
            return {
                str(item["parameter_name"])
                for item in parameters
                if isinstance(item, dict) and item.get("parameter_name")
            }
        return None

    def _get_render_kwargs(self, client) -> Dict[str, Any]:
        """把「唱多长」的模式传给支持它的后端（chorus-mw 0.7.0 起）。

        老后端没有 render_mode 这个参数，按名字探测一下：缺了就整组不传，
        由后端按默认「副歌 45 秒」处理，不会因为多传参数报 invalid keyword。
        """
        parameter_names = self._client_api_parameter_names(client, "/convert")
        if not parameter_names or "render_mode" not in parameter_names:
            if self.render_mode != "chorus" and not getattr(self, "_render_mode_warned", False):
                self._render_mode_warned = True
                logger.warning(
                    f"当前 RVC 中间层不支持 render_mode，{self.render_mode} 未生效"
                    "（按副歌模式处理）；把 chorus-mw 升级到 0.7.0 并重启 3333 后可用"
                )
            return {}
        values = {"render_mode": self.render_mode}
        if "chorus_seconds" in parameter_names:
            values["chorus_seconds"] = self.chorus_seconds
        return values

    def _get_fade_kwargs(self, client) -> Dict[str, Any]:
        """首尾淡入淡出：开关打开且网关支持 fade_seconds 时传 0.5 秒。"""
        if not getattr(self, "enable_fade", True):
            return {}
        parameter_names = self._client_api_parameter_names(client, "/convert")
        if not parameter_names or "fade_seconds" not in parameter_names:
            if not getattr(self, "_fade_warned", False):
                self._fade_warned = True
                logger.warning(
                    "当前网关不支持 fade_seconds，首尾淡入淡出未生效；"
                    "重建 rvc-gateway 后可用")
            return {}
        return {"fade_seconds": 0.5}

    def _get_reverb_kwargs(self, client) -> Dict[str, Any]:
        """人声混响：开关打开且网关支持 reverb_* 参数时传入（读取即时生效，无需重载）。

        CUSTOM-7：网关升级后混响内核是卷积混响（合成 IR），新增 room/damping/
        pre_delay/width 四个参数；按名字探测，老网关缺哪个就不传哪个。
        legacy aecho 回声（echo_*）与 reverb 互斥：reverb 开启时忽略 echo。
        """
        reverb_on = bool(self.config.get("reverb_enabled", False))
        echo_on = bool(getattr(self, "echo_enabled", False)) and not reverb_on
        if not reverb_on and not echo_on:
            return {}
        parameter_names = self._client_api_parameter_names(client, "/convert")
        needed = ("reverb_enabled", "reverb_wet", "reverb_dry", "reverb_decay")
        if not parameter_names or any(n not in parameter_names for n in needed):
            if not getattr(self, "_reverb_warned", False):
                self._reverb_warned = True
                logger.warning(
                    "当前网关不支持 reverb_* 参数，人声混响未生效；重建 rvc-gateway 后可用")
            return {}
        values: Dict[str, Any] = {}
        if reverb_on:
            values.update({
                "reverb_enabled": True,
                "reverb_wet": float(self.config.get("reverb_wet", 0.18) or 0.18),
                "reverb_dry": float(self.config.get("reverb_dry", 1.0) or 1.0),
                "reverb_decay": float(self.config.get("reverb_decay", 1.4) or 1.4),
            })
            extended = {
                "reverb_room_size": getattr(self, "reverb_room_size", 0.65),
                "reverb_damping": getattr(self, "reverb_damping", 0.45),
                "reverb_pre_delay": getattr(self, "reverb_pre_delay", 18.0),
                "reverb_width": getattr(self, "reverb_width", 0.8),
            }
            values.update({k: v for k, v in extended.items() if k in parameter_names})
        elif echo_on and "echo_enabled" in parameter_names:
            values.update({
                "echo_enabled": True,
                "echo_wet": float(getattr(self, "echo_wet", 0.15)),
                "echo_decay": float(getattr(self, "echo_decay", 0.4)),
            })
        return values

    def _get_mix_kwargs(self, client) -> Dict[str, Any]:
        """把采样/混音参数按名字传给支持它们的后端（chorus-mw 0.8.0 起）。

        这几个是 0.8.0 才加到 /convert 的：按名字探测，后端没有就不传，
        由后端用自己的默认值，避免老后端因为「不认识的参数」整次失败。
        传过去的都是真实配置值，不是占位符。
        """
        # CUSTOM-9：实时读配置
        values = {
            "rms_mix_rate": _bounded_float(self.config, "rms_mix_rate", 0.35, 0.0, 1.0),
            "protect": _bounded_float(self.config, "protect", 0.33, 0.0, 0.5),
            "inst_volume_db": _bounded_float(self.config, "inst_volume_db", -6.0, -20.0, 0.0),
            "vocal_volume_db": _bounded_float(self.config, "vocal_volume_db", 0.0, -12.0, 12.0),
        }
        parameter_names = self._client_api_parameter_names(client, "/convert")
        if parameter_names is None:
            if not getattr(self, "_mix_kwargs_warned", False):
                self._mix_kwargs_warned = True
                logger.warning(
                    "读不到后端 /convert 的参数表，本次不提交采样/混音参数"
                    "（后端会用自身默认值）；确认 rvc_base_url 可访问后重试"
                )
            return {}
        missing = [key for key in values if key not in parameter_names]
        if missing:
            if not getattr(self, "_mix_kwargs_warned", False):
                self._mix_kwargs_warned = True
                logger.warning(
                    f"当前 RVC 中间层不支持 {', '.join(missing)}，这些设置本次不生效；"
                    "把 chorus-mw 升级到 0.8.0 并重启 3333 后可用"
                )
            return {key: value for key, value in values.items() if key in parameter_names}
        return values


    async def _get_msst_models_from_client(self, client) -> List[Dict[str, str]]:
        result = await self._async_predict(
            client, api_name="/show_msst_models", timeout=30
        )
        if not isinstance(result, list):
            raise ValueError(f"后端返回格式错误: {result}")

        models = []
        for item in result:
            if isinstance(item, dict) and item.get("id"):
                models.append({
                    "id": str(item["id"]),
                    "name": str(item.get("name") or item["id"]),
                })
            elif isinstance(item, str):
                models.append({"id": item, "name": item})
        if not models:
            raise ValueError("后端没有发现已安装的 MSST 模型")
        return models

    async def _get_optional_msst_kwargs(self, client, base_url: str) -> Dict[str, Any]:
        """Only send the new parameter to an MSST-capable backend."""
        supports = self._client_supports_api(client, "/show_msst_models")
        if supports is True:
            return {"msst_model": self.msst_default_model}
        if supports is False:
            logger.info(f"后端 {base_url} 没有 MSST 模型接口，本次不提交 MSST 参数")
            return {}

        try:
            await self._get_msst_models_from_client(client)
            return {"msst_model": self.msst_default_model}
        except Exception:
            logger.info(f"后端 {base_url} 无法确认 MSST 能力，省略 msst_model 参数")
            return {}

    async def _get_msst_models_from_api(self) -> List[Dict[str, str]]:
        """读取 RVC 后端公开的 MSST 分离模型白名单。"""
        base_url = self._gateway_base_url()
        async with self._get_gradio_client(base_url) as client:
            supports = self._client_supports_api(client, "/show_msst_models")
            if supports is False:
                raise RuntimeError("当前 RVC 后端没有 MSST 模型列表接口")
            models = await self._get_msst_models_from_client(client)
        logger.info(
            f"[MSST模型列表Debug] 插件已从 {base_url} 获取 "
            f"{len(models)} 个模型: {', '.join(item['id'] for item in models)}"
        )
        return models

    async def _sync_msst_model_selection(self, model_id: str) -> Optional[str]:
        """通知 RVC 中间层切换分离模型，返回确认的地址（没确认就给 None）。"""
        base_url = self._gateway_base_url()
        try:
            async with self._get_gradio_client(base_url) as client:
                supports = self._client_supports_api(client, "/select_msst_model")
                if supports is False:
                    return None
                result = await self._async_predict(
                    client,
                    model_name=model_id,
                    api_name="/select_msst_model",
                    timeout=30,
                )
                if isinstance(result, dict) and result.get("success"):
                    logger.info(
                        f"[MSST模型切换Debug] 中间层 {base_url} 已确认切换为 {model_id}"
                    )
                    return base_url
        except Exception as exc:
            logger.debug(f"同步 MSST 模型到后端 {base_url} 失败，已忽略: {exc}")
        return None

    # ==================== 命令处理（支持LLM强制模式） ====================

    @filter.command("翻唱帮助")
    async def cover_help(self, event: AstrMessageEvent):
        """Show the RVC-only entry points for the cover workflow.

        Args:
            event: Message requesting the help card.
        """
        await self.selection_ui.send_card(
            event,
            "🎵 RVC 翻唱",
            "翻唱：/rvc <歌名> [升降调]\n"
            "本地音频：发送音频后使用 /本地翻唱\n\n"
            "取歌按插件配置的 source_order 依次试\n"
            "网易云 → 酷狗 → 酷我 → QQ 音乐。\n"
            "点歌后依次选择歌曲和音色，翻唱完成后发送音频；\n"
            "选择菜单支持按钮、序号、翻页和取消。\n"
            "LLM 强制模式下，请直接说“帮我翻唱《歌名》”。",
            actions=[
                ("RVC 音色", "刷新rvc模型"),
                ("分离模型", "列出msst模型"),
                ("翻唱任务", "查看翻唱任务"),
                ("我的统计", "我的翻唱统计"),
            ],
        )
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("补发翻唱")
    async def resend_cover(self, event: AstrMessageEvent):
        """补发已生成的翻唱成品（插件重载/中断导致没发出去时用）。"""
        raw = re.sub(r"^/?补发翻唱\s*", "", str(event.message_str or "").strip(),
                     flags=re.IGNORECASE).strip()
        if not raw:
            out_dir = self._gateway_out_dir()
            if not out_dir:
                yield event.plain_result("未配置 gateway_work_dir（网关工作目录），补发功能不可用；请在插件设置里填写。")
                return
            recent = list_recent_outputs(out_dir)
            if not recent:
                yield event.plain_result("成品目录里没有可补发的文件（或未配置 gateway_work_dir）")
                return
            lines = ["最近的翻唱成品（按时间倒序）："]
            for p in recent:
                try:
                    import datetime as _dt
                    mt = _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
                    size = p.stat().st_size / 1048576
                    lines.append(f"  {p.name}  {mt}  {size:.1f}MiB")
                except OSError:
                    lines.append(f"  {p.name}")
            lines.append("\n用法：/补发翻唱 <task_id 或文件名>（例：/补发翻唱 bc241ced8e31）")
            yield event.plain_result("\n".join(lines))
            return
        out_dir = self._gateway_out_dir()
        if not out_dir:
            yield event.plain_result("未配置 gateway_work_dir（网关工作目录），补发功能不可用。")
            return
        path, reason = resolve_resend_file(raw, out_dir=out_dir)
        if path is None:
            yield event.plain_result(f"❌ {reason}")
            return
        logger.info("[补发翻唱] 发送成品: %s（来源=命令）", path)
        await self._send_cover_result(event, str(path), song_name=path.stem, record=False)
        try:
            await self._send_progress_notice(
                event, step=f"已补发成品：{path.name}", song=path.stem)
        except Exception:
            pass

    @filter.command("查看翻唱缓存")
    async def show_cover_cache(self, event: AstrMessageEvent):
        """查看 RVC 中间层当前缓存占用。"""
        if not self._parse_cache_target(event.message_str):
            yield event.plain_result("用法：/查看翻唱缓存")
            return
        yield event.plain_result("🔎 正在读取中间层缓存状态，请稍候...")
        lines = []
        try:
            info = await self._backend_cache_request(clear=False)
            if not isinstance(info, dict):
                raise RuntimeError(f"后端返回了无法识别的数据: {info!r}")
            lines.append(
                f"RVC: {int(info.get('total_files', 0))} 个文件 / "
                f"{self._format_cache_bytes(info.get('total_bytes', 0))}"
            )
            for area, stats in (info.get("areas") or {}).items():
                lines.append(
                    f"  - {area}: {int(stats.get('files', 0))} 个 / "
                    f"{self._format_cache_bytes(stats.get('bytes', 0))}"
                )
        except Exception as exc:
            lines.append(f"RVC: 无法读取（{exc}）")
        await self.selection_ui.send_card(
            event, "📦 翻唱缓存", "📦 翻唱缓存状态：\n" + "\n".join(lines)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清理翻唱缓存")
    async def clear_cover_cache(self, event: AstrMessageEvent):
        """清理 RVC 中间层的结果、分离和下载缓存。"""
        if not self._parse_cache_target(event.message_str):
            yield event.plain_result("用法：/清理翻唱缓存")
            return
        yield event.plain_result("🧹 正在安全清理中间层缓存，请稍候...")
        lines = []
        try:
            result = await self._backend_cache_request(clear=True)
            if not isinstance(result, dict):
                raise RuntimeError(f"后端返回了无法识别的数据: {result!r}")
            lines.append(
                f"RVC: 删除 {int(result.get('deleted_files', 0))} 个文件，"
                f"释放 {self._format_cache_bytes(result.get('freed_bytes', 0))}"
            )
        except Exception as exc:
            lines.append(f"RVC: 清理失败（{exc}）")
        await self.selection_ui.send_card(
            event, "🧹 缓存清理结果", "✅ 缓存清理完成：\n" + "\n".join(lines)
        )
    @filter.command("列出msst模型")
    async def list_msst_models(self, event: AstrMessageEvent):
        """列出当前 RVC 中间层安装的人声分离模型。"""
        yield event.plain_result("🔄 正在读取 MSST 模型列表，请稍候...")
        try:
            models = await self._get_msst_models_from_api()
        except Exception as exc:
            yield event.plain_result(f"ℹ️ 当前后端没有可列出的 MSST 模型：{exc}")
            return

        lines = [
            f"{index}. {item['name']}\n   {item['id']}"
            + ("  ← 当前默认" if item["id"] == self.msst_default_model else "")
            for index, item in enumerate(models, 1)
        ]
        await self.selection_ui.send_card(
            event,
            "🎛️ MSST 分离模型",
            "可用的 MSST 人声分离模型：\n"
            + "\n".join(lines)
            + "\n\n管理员可使用：/切换msst模型 <序号或模型名>",
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换msst模型")
    async def switch_msst_model(self, event: AstrMessageEvent):
        """查看或切换 RVC 中间层的默认人声分离模型。"""
        raw = event.message_str.strip()
        requested = re.sub(r"^/?切换msst模型\s*", "", raw, flags=re.IGNORECASE).strip()

        yield event.plain_result("🔄 正在读取 MSST 模型列表，请稍候...")
        try:
            models = await self._get_msst_models_from_api()
        except Exception as exc:
            logger.error(f"获取 MSST 模型列表失败: {exc}")
            yield event.plain_result(f"❌ 无法读取 MSST 模型列表：{exc}")
            return

        lines = [
            f"{index}. {item['name']}\n   {item['id']}"
            + ("  ← 当前默认" if item["id"] == self.msst_default_model else "")
            for index, item in enumerate(models, 1)
        ]
        if not requested:
            yield event.plain_result(
                "当前 MSST 默认模型："
                f"{self.msst_default_model}\n\n可用模型：\n"
                + "\n".join(lines)
                + "\n\n用法：/切换msst模型 <序号或模型名>"
            )
            return

        selected = None
        if requested.isdigit():
            model_index = int(requested) - 1
            if 0 <= model_index < len(models):
                selected = models[model_index]
        else:
            lowered = requested.lower()
            exact = [
                item for item in models
                if lowered in (item["id"].lower(), item["name"].lower())
            ]
            partial = [
                item for item in models
                if lowered in item["id"].lower() or lowered in item["name"].lower()
            ]
            candidates = exact or partial
            if len(candidates) == 1:
                selected = candidates[0]

        if selected is None:
            yield event.plain_result(
                f"❌ 没有唯一匹配的 MSST 模型：{requested}\n\n" + "\n".join(lines)
            )
            return

        self.msst_default_model = selected["id"]
        synced_backend = await self._sync_msst_model_selection(self.msst_default_model)
        sync_text = f"\n中间层确认：{synced_backend}" if synced_backend else ""
        await self.selection_ui.send_card(
            event,
            "✅ MSST 模型设置",
            f"✅ 已通知中间层切换分离模型：{selected['name']}\n"
            f"模型 ID：{selected['id']}\n"
            "注意：chorus-mw 0.8.0 的分离模型由后端启动配置固定，"
            "这个选择只在声明支持 msst_model 参数的中间层上生效。" + sync_text,
        )

    def _models_dir_note(self) -> str:
        """把「音色模型 / 索引放在哪个文件夹」压成几行给人看（0.12.0 起带索引目录）。"""
        info = getattr(self, "_models_dir_info", None) or {}
        directory = info.get("dir") or self.rvc_models_dir
        if not directory:
            return ""
        lines = [f"\n📁 音色模型：{directory}"]
        if info:
            lines.append(
                f"（{info.get('count', 0)} 个 .pth，来源：{info.get('source', '中间层')}）"
            )
        elif self.rvc_models_dir:
            lines.append("（还没核对过，发一次 /刷新rvc模型 可以确认中间层是否在用这个目录）")
        index_dirs = info.get("index_dirs") or []
        if index_dirs:
            lines.append("🗂 索引文件：" + "；".join(index_dirs))
        warning = info.get("warning")
        if warning:
            lines.append(f"\n⚠️ {warning}")
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("rvc模型文件夹")
    async def open_rvc_models_dir(self, event: AstrMessageEvent):
        """显示音色模型和索引的真实目录，并直接在跑机器人的这台机器上打开模型文件夹。"""
        try:
            info, _models = await self._fetch_models_info()
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 拿不到模型目录：{e}")
            return

        directory = info.get("dir") or self.rvc_models_dir
        if not directory:
            yield event.plain_result("❌ 中间层没返回模型目录，先确认 3333 在跑、版本 ≥0.9.0。")
            return

        lines = [f"📁 音色模型：{directory}"]
        if info:
            lines.append(f"（{info.get('count', 0)} 个 .pth，来源：{info.get('source', '中间层')}）")
        index_dirs = info.get("index_dirs") or []
        if index_dirs:
            lines.append("🗂 索引文件：" + "；".join(index_dirs))
        lines.append("把 .pth 丢进上面那个目录，.index 放进索引目录，然后发 /刷新rvc模型。")

        opened = False
        try:
            if os.name == "nt" and os.path.isdir(directory):
                os.startfile(directory)  # noqa: S606  只在管理员命令里打开，路径来自中间层
                opened = True
        except Exception as e:
            logger.warning(f"打开模型文件夹失败: {e}")
        if opened:
            lines.append("（已在这台机器上把文件夹打开）")
        if info.get("warning"):
            lines.append(f"⚠️ {info['warning']}")

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置默认音色")
    async def set_default_model(self, event: AstrMessageEvent):
        """设置默认音色：/设置默认音色 deng（不带参数查看当前值）。"""
        raw = re.sub(r"^/?设置默认音色\s*", "", str(event.message_str or "").strip(),
                     flags=re.IGNORECASE).strip()
        models = [m["filename"] for m in self.get_models_detailed_list()]
        if not raw:
            current = str(self.config.get("rvc_default_model", "") or "").strip()
            yield event.plain_result(
                "当前默认音色：" + (current or "（未设置，按第一个模型）") +
                "\n可用模型：" + (", ".join(models) or "（列表为空，先 /刷新rvc模型）") +
                "\n用法：/设置默认音色 <模型名，可省略 .pth>")
            return
        token = raw.split()[0].strip().lower()
        base = token[:-4] if token.endswith(".pth") else token
        hit = ""
        for filename in models:
            if base in (filename.lower(), os.path.splitext(filename)[0].lower()):
                hit = os.path.splitext(filename)[0]
                break
        if not hit:
            yield event.plain_result(
                f"❌ 没有名为 {raw} 的模型。可用：" + (", ".join(models) or "（空）"))
            return
        self.config["rvc_default_model"] = hit
        self.config.save_config()
        self.rvc_default_model = hit
        logger.info("默认音色已更新: %s（来源=命令，写入 rvc.models.rvc_default_model）", hit)
        yield event.plain_result(
            f"✅ 默认音色 → {hit}（已保存）。之后 /rvc 与自然语言翻唱都用它，不再弹音色菜单。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置模型性别")
    async def set_model_gender(self, event: AstrMessageEvent):
        """模型性别配置：/设置模型性别 <模型名> <男声|女声|未知>，不带参数查看当前值。"""
        raw = re.sub(r"^/?设置模型性别\s*", "", str(event.message_str or "").strip(),
                     flags=re.IGNORECASE).strip()
        if not raw:
            lines = ["当前模型性别（也可在 WebUI「模型性别」下拉直接改）："]
            for m in self.get_models_detailed_list():
                base = os.path.splitext(m["filename"])[0]
                g = self.model_gender_map.get(base, "unknown")
                lines.append(f"  {base}.pth → {self._gender_label(g)}")
            lines.append("用法：/设置模型性别 <模型名> <男声|女声|未知>")
            yield event.plain_result("\n".join(lines))
            return
        parts = raw.split()
        if len(parts) < 2:
            yield event.plain_result("用法：/设置模型性别 <模型名> <男声|女声|未知>")
            return
        name = parts[0].strip().lower().removesuffix(".pth")
        g = self._norm_gender(" ".join(parts[1:]))
        if g is None:
            yield event.plain_result("性别只支持：男声 / 女声 / 未知")
            return
        self._write_model_gender(name, g)
        logger.info("模型性别已更新: %s.pth -> %s（来源=命令，写入 model_genders）", name, g)
        yield event.plain_result(
            f"✅ 模型 {name}.pth 性别 → {self._gender_label(g)}"
            "（已保存到统一配置 model_genders，立即生效，不再为它调用 LLM）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置歌手性别")
    async def set_artist_gender(self, event: AstrMessageEvent):
        """歌手性别配置：/设置歌手性别 <歌手> <男声|女声|未知>，不带参数查看当前值。"""
        raw = re.sub(r"^/?设置歌手性别\s*", "", str(event.message_str or "").strip(),
                     flags=re.IGNORECASE).strip()
        if not raw:
            lines = ["当前歌手性别映射（优先级最高，未配置的歌手才走 LLM 判断）："]
            if self.artist_gender_map:
                for k, v in self.artist_gender_map.items():
                    lines.append(f"  {k} → {self._gender_label(v)}")
            else:
                lines.append("  （空，全部由 LLM 判断）")
            lines.append("用法：/设置歌手性别 <歌手> <男声|女声|未知>")
            yield event.plain_result("\n".join(lines))
            return
        parts = raw.split()
        if len(parts) < 2:
            yield event.plain_result("用法：/设置歌手性别 <歌手> <男声|女声|未知>")
            return
        name = parts[0].strip()
        g = self._norm_gender(" ".join(parts[1:]))
        if g is None:
            yield event.plain_result("性别只支持：男声 / 女声 / 未知")
            return
        entries = [
            e for e in (self.config.get("artist_gender_map", []) or [])
            if not (isinstance(e, str) and e.split(":", 1)[0].strip().lower() == name.lower())
        ]
        if g != "unknown":
            entries.append(f"{name}:{g}")
        self.config["artist_gender_map"] = entries
        self.artist_gender_map = self._parse_gender_map(entries)
        self._gender_cache.clear()
        self.config.save_config()
        logger.info("歌手性别已更新: %s -> %s（来源=命令配置）", name, g)
        yield event.plain_result(
            f"✅ 歌手 {name} 性别 → {self._gender_label(g)}（已保存，优先于 LLM 判断）")

    @filter.command("刷新rvc模型")
    async def refresh_rvc_models(self, event: AstrMessageEvent):
        yield event.plain_result("正在刷新 RVC 模型列表，请稍候...")
        try:
            result = await self._update_models_from_api()
            if not result["ok"]:
                yield event.plain_result(f"刷新失败：{result['reason']}")
                return
            if result["missing_default"]:
                yield event.plain_result(
                    f"刷新成功！⚠️ 默认音色 {result['missing_default']} 不在新列表里，"
                    "配置值已原样保留（未清空），请在插件配置里重新选择。")
            else:
                yield event.plain_result("刷新成功！")
            display_str, _ = self.get_models_display_list()
            display_str = display_str or "未发现任何模型。"
            note = self._models_dir_note()
            await self.selection_ui.send_list(event, f"当前 RVC 可用模型：\n{display_str}{note}")
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"刷新 RVC 模型出错了: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置rvc后端链接")
    async def set_rvc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置rvc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(f"当前 RVC 后端: {self.rvc_base_url}\n用法: /设置rvc后端链接 <URL>")
            return
        _url = args[0]
        if not _url.endswith("/"): _url += "/"
        self.rvc_base_url = _url
        self.config["rvc_base_url"] = _url
        self.config.save_config()
        yield event.plain_result(f"RVC 后端链接已设置为: {_url}")

    @filter.command("rvc")
    async def rvc(self, event: AstrMessageEvent):
        if not self.enable_rvc:
            yield event.plain_result("❌ RVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_rvc 为 true。")
            return
        if self.llm_force_mode:
            yield event.plain_result("当前已开启 LLM 强制模式，/rvc 命令已被禁用。\n请直接对我说'翻唱《歌名》'，我会自动帮您处理！")
            return
        async for result in self._handle_cover(event):
            yield result

    @filter.command("本地翻唱")
    async def local_audio_cover_cmd(self, event: AstrMessageEvent):
        if not self.enable_local_audio_cover:
            yield event.plain_result("❌ 本地音频翻唱功能未启用。")
            return

        audio_path = await self._extract_audio_from_event(event)

        if not audio_path:
            await self._cleanup_expired_audio_cache()
            cached = self._recent_audio_cache.get(str(event.unified_msg_origin))
            if cached and os.path.exists(cached[0]):
                audio_path = cached[0]

        if not audio_path:
            yield event.plain_result(
                "❌ 未检测到音频文件或语音消息。\n\n用法：\n"
                "1. 在同一条消息中发送音频+文字 /本地翻唱\n"
                "2. 先发送语音/音频，然后在5分钟内发送 /本地翻唱\n\n"
                "可选参数：[模型名/序号] [升降调]\n"
                "示例：/本地翻唱 官方示例音色 0"
            )
            return

        args = event.message_str.replace("本地翻唱", "").strip().split()
        model_name = None
        key_shift = self.default_key_shift

        for arg in args:
            if arg.lstrip('-').isdigit() and -12 <= int(arg) <= 12:
                key_shift = int(arg)
            elif len(arg) >= 1:
                model_name = arg

        await self._execute_local_cover(
            event, audio_path, model_name, key_shift=key_shift
        )


    async def _handle_cover(self, event: AstrMessageEvent):
        args = re.sub(
            r"^/?rvc\s*", "", str(event.message_str or "").strip(), flags=re.IGNORECASE
        ).strip().split()

        if not args:
            await self.selection_ui.send_card(
                event,
                "🎵 RVC 翻唱",
                "用法：/rvc <歌名> [升降调]\n"
                "示例：/rvc 优しい詩。\n\n"
                "输入歌名后，按卡片选择歌曲，再选择音色。\n"
                "发送 /翻唱帮助 查看所有入口。",
            )
            return

        key_shift, song_name = 0, " ".join(args)
        if args and args[-1].lstrip("-").isdigit():
            try:
                val = int(args[-1])
                if -12 <= val <= 12:
                    key_shift = val
                    song_name = " ".join(args[:-1]) if len(args) > 1 else ""
            except ValueError:
                pass

        if not song_name:
            yield event.plain_result("请输入歌名！")
            return

        # 0.7.0：按 source_order 搜歌，哪个平台先有结果就用哪个（默认网易云）
        candidates, search_reason = await self.song_router.search_candidates(
            song_name, limit=10
        )
        if not candidates:
            logger.info(f"所有音源搜歌都没结果: {song_name}（{search_reason}）")
            yield event.plain_result("没能找到这首歌~")
            return
        # 菜单里也不展示纯伴奏版本：选它翻唱只会出静音
        playable = [
            candidate
            for candidate in candidates
            if not source_router.is_accompaniment(candidate.name, candidate.alias)
        ]
        if not playable:
            logger.info(f"搜到的候选全是伴奏版: {song_name}")
            await self.outbound.say(
                event,
                "failed",
                song=song_name,
                reason="只有伴奏版，找不到有人声的版本",
            )
            return
        candidates = playable
        songs = [candidate.as_dict() for candidate in candidates]
        source_display = source_router.SOURCE_DISPLAY.get(
            candidates[0].source, "网易云音乐"
        )

        def menu_label(song: dict) -> str:
            label = f"{song['name']} - {song['artists']}"
            if song.get("alias"):
                label += f"（{song['alias']}）"
            return label

        # === 自动选歌：逐源搜+逐源挑，第一个能自信选出的源直接用 ===
        # 全部源都区分度不足 → 用第一个有结果的源的候选弹菜单（原行为），不硬猜。
        selected_song = None
        auto_pick_reason = ""
        pick_score = 0.0
        if self.auto_pick_song:
            pick = await self._auto_pick_from_sources(song_name, flow_id="rvc")
            if pick["all_accompaniment"]:
                await self.outbound.say(
                    event,
                    "failed",
                    song=song_name,
                    reason="只有伴奏版，找不到有人声的版本",
                )
                return
            selected_song = pick["selected"]
            songs = pick["songs"] or songs
            if pick["source_display"]:
                source_display = pick["source_display"]
            auto_pick_reason = pick["picked_reason"]
            pick_score = pick["pick_score"]
            if selected_song is None and not self.auto_pick_fallback_menu:
                await self.outbound.say(
                    event,
                    "failed",
                    song=song_name,
                    reason="候选区分度不足，自动选歌未开启菜单回退",
                )
                return

        try:
            if selected_song is None:
                selected_song_index, event = await self.selection_ui.choose(
                    event,
                    f"🎵 RVC · 选择歌曲\n来源：{source_display}",
                    [menu_label(song) for song in songs],
                    self.timeout,
                )
                if selected_song_index is None:
                    await event.send(event.plain_result("已取消选歌。"))
                    return
                selected_song = songs[selected_song_index]
            display_str, keys = self.get_models_display_list()
            if not keys:
                yield event.plain_result(
                    "当前没有可用的 RVC 模型，请先使用 /刷新rvc模型。"
                )
                return

            # === 默认音色：配置了就用配置的，没配/匹配不上就退回菜单 ===
            selected_model_index = None
            if self.auto_pick_model:
                configured_index = self._configured_rvc_model_index()
                if configured_index and 1 <= configured_index <= len(keys):
                    selected_model_index = configured_index - 1
                    logger.info(
                        "[默认音色] 直接使用配置默认音色：%s（序号 %d）",
                        keys[selected_model_index], configured_index)
                elif configured_index:
                    logger.warning(
                        "[默认音色] 配置的序号 %d 超出模型数量 %d，退回菜单",
                        configured_index, len(keys))
            if selected_model_index is None:
                selected_model_index, event = await self.selection_ui.choose(
                    event,
                    f"🎙️ RVC · 选择音色\n已选歌曲：{selected_song['name']}\n歌手：{selected_song['artists']}"
                    + (f"\n自动选歌：{auto_pick_reason}" if auto_pick_reason else ""),
                    [line.split(". ", 1)[1] for line in display_str.splitlines()],
                    self.timeout,
                )
        except TimeoutError:
            yield event.plain_result("选择超时，操作已取消。")
            return
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        if selected_model_index is None:
            await event.send(event.plain_result("已取消音色选择。"))
            return

        selected_model = keys[selected_model_index]

        await self.selection_ui.send_card(
            event,
            "🎙️ 开始翻唱",
            f"歌曲：{selected_song['name']}\n歌手：{selected_song['artists']}\n"
            f"音色：{selected_model}\n来源：{source_display}\n"
            f"升降调：{key_shift}\n\n"
            "已开始生成，完成后会发送音频。",
        )
        await self._send_song(
            event=event,
            song=selected_song,
            model_name=selected_model,
            key_shift=key_shift,
        )


    async def initialize(self):
        """Bind QQ callback handlers to platform clients already loaded."""
        self.selection_ui.bind_platforms(self.context.platform_manager.platform_insts)

    @filter.on_platform_loaded(priority=sys.maxsize)
    async def on_qq_platform_loaded(self):
        """Bind callbacks before newly loaded QQ clients connect."""
        await self.initialize()

    @filter.command("翻唱选择")
    async def expired_selection(
        self, event: AstrMessageEvent, token: str = "", choice: str = ""
    ):
        """Explain command buttons that have no active selection waiter.

        Args:
            event: Message containing a stale or unauthorized button command.
            token: Menu identifier retained by the old button.
            choice: Selection action retained by the old button.
        """
        yield event.plain_result(
            "这个选择菜单已过期，或不属于你。请重新发送点歌命令，再使用新菜单。"
        )

    async def _send_song(self, event: AstrMessageEvent, song: dict, model_name: str, key_shift: Optional[int]):
        result_path = None
        temp_audio_file = None
        registry_error = ""
        registry_timeout = False
        song_name = song.get("name", "翻唱")
        if not self._register_active_task(event, song_name, model_name):
            await event.send(event.plain_result(
                self._last_register_reject_reason or "⏳ 当前会话已有翻唱任务，请等待完成或先取消任务。"))
            return
        self._bind_active_asyncio_task(event)
        try:
            await self._send_progress_notice(event, step="正在取歌", song=song_name)
            # 统一取歌入口：卡片/命令选中的歌曲（各平台 id、甚至本地文件）
            # 都在这里变成"中间层能直接吃的本地音频"，绝不把 id 原样传下去。
            song_input, prepare_reason, _prepare_trial = await self._prepare_song_audio(
                event, song
            )
            if not song_input:
                logger.error(f"❌ 取歌失败（song={song_name}）: {prepare_reason}")
                registry_error = f"取歌失败: {prepare_reason}"
                # 只把事实交给模型：所有音源都只剩伴奏版时，让它说「只有伴奏版」
                reason = (
                    "只有伴奏版，找不到有人声的版本"
                    if prepare_reason == source_router.REASON_ONLY_ACCOMPANIMENT
                    else f"这首拿不到音源（{prepare_reason}）"
                )
                await self.outbound.say(
                    event,
                    "failed",
                    song=song_name,
                    model=model_name,
                    reason=reason,
                )
                return
            if self._is_temp_audio(song_input):
                # 插件自己下的临时文件交给 finally 清理；用户给的本地文件不能删
                temp_audio_file = song_input
            await self._send_progress_notice(
                event,
                step=(
                    f"音源准备好了，正在提交中间层"
                    f"（{os.path.getsize(song_input) / 1024 / 1024:.1f} MiB）"
                ),
                song=song_name,
            )

            __uid, __uname = self._sender_pair(event)
            self._pending_cover_meta[getattr(event, "unified_msg_origin", "")] = {
                "user": __uid, "user_name": __uname, "song": song_name, "artist": "", "model": model_name}
            result_path, cache_hit = await self._predict_cover(
                event=event,
                song_name_src=song_input,
                model_name=model_name,
                key_shift=key_shift,
            )

            if result_path and os.path.exists(result_path):
                await self._send_cover_result(
                    event, result_path, song_name=song_name, cache_hit=cache_hit,
                    model=model_name)
            else:
                registry_error = "生成失败，后端没有返回有效的音频文件"
                await self.outbound.say(event, "failed", song=song_name, model=model_name, reason=registry_error)
        except Exception as e:
            logger.error(traceback.format_exc())
            if "Timeout" in str(e):
                registry_error = f"生成超时，后端 {self.inference_timeout} 秒内没做完"
                registry_timeout = True
                await self.outbound.say(event, "failed", song=song_name, model=model_name, reason=registry_error)
            else:
                registry_error = str(e)
                await self.outbound.say(event, "failed", song=song_name, model=model_name, reason=str(e))
        finally:
            self._finish_active_task(event, error=registry_error, timeout=registry_timeout)
            if self.enable_send_file:
                await asyncio.sleep(3)
            if result_path and os.path.isfile(result_path):
                try: os.remove(result_path)
                except OSError as e: logger.debug(f"删除临时文件失败（文件可能仍被占用）: {e}")
            if temp_audio_file and os.path.isfile(temp_audio_file):
                try: os.remove(temp_audio_file)
                except OSError as e: logger.error(f"删除临时音频文件失败: {e}")


    async def _execute_local_cover(self, event: AstrMessageEvent, audio_path: str,
                                    model_name: Optional[str] = None,
                                    model_index: Optional[int] = None,
                                    key_shift: Optional[int] = 0):
        """本地音频翻唱核心执行逻辑（只用 RVC）"""
        if not os.path.exists(audio_path):
            await event.send(event.plain_result("❌ 音频文件不存在或已过期，请重新发送音频。"))
            return

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > self.max_local_audio_size_mb:
            await event.send(event.plain_result(f"❌ 音频文件过大 ({size_mb:.1f}MB)，超过限制 {self.max_local_audio_size_mb}MB。"))
            return

        if not self.enable_rvc:
            await event.send(event.plain_result("❌ RVC 功能已在配置中禁用。"))
            return

        models_keywords = self.rvc_models_keywords
        if not models_keywords:
            await event.send(event.plain_result(
                "❌ 当前没有可用的 RVC 模型，请先使用 /刷新rvc模型。"
            ))
            return

        # 配置下拉选的默认 RVC 音色：调用方没给模型名也没给序号时优先用它
        if not model_name and model_index is None:
            model_index = self._configured_rvc_model_index() or 1

        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if matched:
                model_index = matched
            else:
                await event.send(event.plain_result(
                    f"❌ 未找到匹配的 RVC 模型：'{model_name}'"
                ))
                return

        if not (1 <= model_index <= len(models_keywords)):
            await event.send(event.plain_result(f"❌ 模型序号无效，当前共有 {len(models_keywords)} 个模型。"))
            return

        if key_shift is not None and not (-12 <= key_shift <= 12):
            await event.send(event.plain_result("❌ 音调调整值无效，请在 -12 到 12 之间选择。"))
            return

        _, keys = self.get_models_display_list()
        selected_model = keys[model_index - 1]
        models_info = self.get_models_detailed_list()
        model_display = ""
        for m in models_info:
            if m["index"] == model_index:
                model_display = m["display"]
                break

        song_display_name = os.path.splitext(os.path.basename(audio_path))[0]
        if song_display_name.startswith("qq_") or song_display_name.startswith("dl_"):
            song_display_name = "本地音频"

        if not self._register_active_task(event, song_display_name, model_display):
            await event.send(event.plain_result(
                self._last_register_reject_reason or "⏳ 当前会话已有翻唱任务，请等待完成或先取消任务。"))
            return
        self._bind_active_asyncio_task(event)

        result_path = None
        try:
            await self.selection_ui.send_card(
                event,
                "🎙️ 本地音频翻唱",
                f"🎵 正在用【{model_display}】处理您的音频，请稍候...",
            )

            __uid, __uname = self._sender_pair(event)
            self._pending_cover_meta[getattr(event, "unified_msg_origin", "")] = {
                "user": __uid, "user_name": __uname, "song": song_display_name, "artist": "", "model": selected_model}
            result_path, cache_hit = await self._predict_cover(
                event=event,
                song_name_src=audio_path,
                model_name=selected_model,
                key_shift=key_shift,
            )

            if result_path and os.path.exists(result_path):
                await self._send_cover_result(
                    event, result_path, song_name=song_display_name, cache_hit=cache_hit,
                    model=selected_model)

                result_msg = f"✅ 已成功使用 RVC 音色【{selected_model}】处理您的音频！"
                if cache_hit:
                    result_msg = f"⚡ 命中缓存！{result_msg}"
                if self.enable_config_report:
                    result_msg += f"\n\n📊 本次配置：模型={model_display}, 调音={int(key_shift or 0):+d}"
                if self.enable_config_report:
                    await self.selection_ui.send_card(event, "✅ 翻唱完成", result_msg)
                await self._notify_llm_with_context(
                    event, song_display_name, "cache_hit" if cache_hit else "success")
            else:
                await event.send(event.plain_result("❌ 处理失败，后端未返回有效文件路径。"))
                await self._notify_llm_with_context(event, song_display_name, "failed", result_detail="后端未返回文件路径")

        except Exception as e:
            logger.error(traceback.format_exc())
            error_msg = f"处理时发生错误: {e}"
            if "Timeout" in str(e):
                error_msg = f"⏳ 处理超时！后端在 {self.inference_timeout} 秒内没有完成任务。"
            await event.send(event.plain_result(error_msg))
            await self._notify_llm_with_context(event, song_display_name, "failed", result_detail=str(e))
        finally:
            self._finish_active_task(event)
            temp_dir = _temp_audio_dir()
            if audio_path.startswith(temp_dir) and os.path.isfile(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            if result_path and os.path.isfile(result_path):
                try:
                    if self.enable_send_file:
                        await asyncio.sleep(3)
                    os.remove(result_path)
                except OSError:
                    pass


    # ==================== 基础LLM工具（原有功能） ====================
    
    @filter.llm_tool(name="search_music")
    async def search_music(self, event: AstrMessageEvent, keyword: str, limit: int = 5) -> str:
        '''按插件配置的音源顺序搜歌（默认网易云 → 酷狗 → 酷我 → QQ 音乐）。

        Args:
            keyword(string): 搜索关键词，可以是歌曲名、歌手名等
            limit(number): 返回结果数量上限，默认为5，最大为10

        Returns:
            返回搜索到的歌曲列表，包含序号、歌名、歌手和来源音源
        '''
        try:
            limit = min(max(limit, 1), 10)
            candidates, reason = await asyncio.wait_for(
                self.song_router.search_candidates(keyword, limit=limit),
                timeout=self.music_api_timeout,
            )

            if not candidates:
                return f"没找到与 '{keyword}' 相关的歌曲（{reason}）。"

            if self.enable_enhanced_context:
                result_lines = [
                    f"🎵 找到 {len(candidates)} 首相关歌曲：",
                    "",
                    "【歌曲列表】"
                ]
                for i, song in enumerate(candidates, 1):
                    alias = f"（别名：{song.alias}）" if song.alias else ""
                    result_lines.append(
                        f"  {i}. 《{song.name}》 - {song.artists}{alias}"
                        f"\n     来源: {source_router.SOURCE_DISPLAY.get(song.source, song.source)}"
                    )

                result_lines.extend([
                    "",
                    "💡 快捷指令提示：",
                    "- 直接说 '翻唱第X首' 或 '用第Y个模型翻唱第X首'",
                    "- 我会自动帮您完成后续步骤！",
                    "",
                    f"📝 当前状态：已搜索 '{keyword}'，共 {len(candidates)} 首歌曲可选"
                ])
            else:
                result_lines = [f"🎵 找到 {len(candidates)} 首相关歌曲："]
                for i, song in enumerate(candidates, 1):
                    alias = f"（别名：{song.alias}）" if song.alias else ""
                    result_lines.append(
                        f"{i}. 《{song.name}》 - {song.artists}{alias}"
                    )
                result_lines.append("\n请告诉用户搜索结果，并询问他们想要翻唱哪首歌（输入序号）。")

            return "\n".join(result_lines)
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"搜索歌曲时出错: {e}"


    @filter.llm_tool(name="rvc_cover")
    async def rvc_cover(self, event: AstrMessageEvent, song_name: str, artist_name: Optional[str] = None, model_index: int = 1, model_name: Optional[str] = None, key_shift: int = 0) -> str:
        '''使用 RVC 音色模型翻唱歌曲。

        **只在用户明确要「唱 / 翻唱 / 变声」时调用。用户说「点歌」「放首歌」「听」是想放原曲，
        不要用本工具。**

        Args:
            song_name(string): 用户说的歌名，**原样整串**填进来。版本名/别名（司凤版、
                伴奏版、纯享版、DJ版…）属于歌名的一部分，留在 song_name 里。
                例：「唱雨爱司凤版」→ song_name='雨爱司凤版'，artist_name 不填；
                「唱周杰伦的晴天」→ song_name='晴天'，artist_name='周杰伦'。
            artist_name(string, optional): 只有用户**明确说出歌手**时才填
                （「周杰伦的晴天」「晴天 周杰伦 唱的」「歌手是周杰伦」）。
                没说歌手就不要填，不许凭印象或搜索结果猜，也不要把版本名塞进来。
            model_index(number): 使用的模型序号（从可用模型列表中选择），默认为1表示第一个模型
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。优先级高于model_index
                例如：'塔菲'会自动匹配到tafeim.pth
            key_shift(number): 音调调整，范围为-12到12，默认为0

        Returns:
            翻唱结果的描述信息（异步模式会立即返回"正在处理"）

        注意：
            RVC 功能可能已在配置中禁用，此时会返回错误提示。
        '''
        rejected = self._reject_music_request(event, "rvc_cover")
        if rejected:
            return rejected
        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_rvc 为 true。"
        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if matched:
                model_index = matched
            else:
                return f"❌ 未找到匹配的RVC模型: '{model_name}'\n\n💡 提示：使用 get_available_models 查看可用模型"

        models_info = self.get_models_detailed_list()
        model_display = ""
        for m in models_info:
            if m["index"] == model_index:
                model_display = m["display"]
                break

        if not self._register_active_task(event, song_name, model_display):
            return self._last_register_reject_reason or "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"
        logger.info(
            f"[任务Debug] 接受 rvc_cover 请求 | song={song_name} | "
            f"artist={artist_name or '-'} | model={model_display}"
        )
        task = self._create_tracked_task(self._smart_cover_async(
            event, song_name, model_index, key_shift, model_display,
            artist_name=artist_name,
        ))
        self._bind_active_asyncio_task(event, task)
        # 只回一句中性短句给 LLM，具体怎么说由人格模型组织；不再拼符号和参数
        return f"好，我去弄这首《{song_name}》，好了直接发你。（系统会直接把结果发到群里，失败也会另行通知；这条只是受理确认，不要向用户复述、转述或展开它。）"


    async def _do_cover(self, event: AstrMessageEvent, song_name: str, model_index: int, key_shift: Optional[int], artist_name: str = "") -> str:
        """统一的翻唱执行逻辑（只用 RVC）。

        ``song_name`` 原样整串交给搜歌入口；``artist_name`` 非空才当过滤条件，
        由 :func:`source_router.pick_best_song` 统一处理（不再拼搜索串）。
        """
        flow_id = uuid.uuid4().hex[:8]
        result_path = None
        temp_audio_file = None
        try:
            logger.info(
                f"[CoverFlow:{flow_id}] stage=start | song={song_name} | "
                f"model_index={model_index}"
            )
            if not self.enable_rvc:
                return "❌ RVC 功能已在配置中禁用。"
            if key_shift is None:
                key_shift = self.default_key_shift

            models_keywords = self.rvc_models_keywords

            if not models_keywords:
                return "❌ 当前没有可用的 RVC 模型，请先使用 /刷新rvc模型 更新列表。"

            if not (1 <= model_index <= len(models_keywords)):
                return f"❌ 模型序号无效，当前共有 {len(models_keywords)} 个模型可选，请输入 1 到 {len(models_keywords)} 之间的数字。"

            if not (-12 <= key_shift <= 12):
                return "❌ 音调调整值无效，请在 -12 到 12 之间选择。"

            # === 搜歌：0.8.0 起 song_name 整串原样搜，artist 只在明确给出时当过滤条件 ===
            logger.info(
                f"[CoverFlow:{flow_id}] stage=music_search | "
                f"order={source_router.describe_order(self.source_order)} | "
                f"timeout={self.music_api_timeout}s | artist={artist_name or '-'}"
            )
            selected_song = None
            search_reason = ""
            if self.auto_pick_song:
                # 自然语言入口与 /rvc 共用同一套自动选歌（含原唱降权、别名、逐源）
                _pick = await self._auto_pick_from_sources(
                    song_name, artist_name or "", flow_id=flow_id)
                if _pick["all_accompaniment"]:
                    return (
                        f"❌ 《{song_name}》只找到伴奏版（没有人声），翻唱不了，"
                        "换一个有人声的版本再试。"
                    )
                selected_song = _pick["selected"]
                if selected_song is None:
                    logger.info(
                        "[CoverFlow:%s] 自动选歌区分度不足，回退严格匹配 find_song",
                        flow_id)
            if selected_song is None:
                selected_song, search_reason, _search_attempts = (
                    await self.song_router.find_song(song_name, artist_name or "")
                )

            logger.info(
                f"[CoverFlow:{flow_id}] stage=music_search_done | "
                f"hit={bool(selected_song)} | reason={search_reason}"
            )

            if not selected_song:
                logger.info(
                    f"[CoverFlow:{flow_id}] stage=no_confident_match | "
                    f"song={song_name} | reason={search_reason}"
                )
                if search_reason == source_router.REASON_ONLY_ACCOMPANIMENT:
                    return (
                        f"❌ 《{song_name}》只找到伴奏版（没有人声），翻唱不了，"
                        "换一个有人声的版本再试。"
                    )
                return (
                    f"❌ 所有音源都没找到能确认是《{song_name}》的版本"
                    f"（{search_reason}），换个更全的歌名或歌手试试。"
                )

            _, keys = self.get_models_display_list()
            selected_model = keys[model_index - 1]

            # === 自动升降调 ===
            models_info = self.get_models_detailed_list()
            model_display = ""
            for m in models_info:
                if m["index"] == model_index:
                    model_display = m["display"]
                    break

            original_key_shift = key_shift
            artist_from_song = selected_song.get("artists", "")
            artist_gender = None
            model_gender = None
            detection_attempted = False
            if self.enable_auto_key_shift and key_shift == 0:
                detection_attempted = True
                logger.info(f"[CoverFlow:{flow_id}] stage=gender_detection")
                artist_gender, model_gender = await self._detect_gender_pair(
                    artist_from_song, model_display, event
                )
            key_shift = await self._calc_auto_key_shift(
                artist_from_song,
                model_display,
                key_shift,
                artist_gender,
                model_gender,
                event,
                detection_attempted=detection_attempted,
            )
            if key_shift != original_key_shift and self.enable_auto_key_shift:
                logger.info(f"自动升降调：{artist_from_song} → {model_display}，音调从 {original_key_shift:+d} 调整为 {key_shift:+d}")

            # === 统一取歌入口：只把本地音频交给中间层，id 一律不出插件 ===
            song_input_for_api, prepare_reason, prepare_trial = await self._prepare_song_audio(
                event, selected_song
            )
            if not song_input_for_api:
                logger.error(
                    f"[CoverFlow:{flow_id}] stage=audio_prepare_failed | "
                    f"song={selected_song.get('name', song_name)} | "
                    f"trial={prepare_trial} | reason={prepare_reason}"
                )
                return f"❌ 这首拿不到音源（{prepare_reason}）"
            if self._is_temp_audio(song_input_for_api):
                # 插件自己下的临时文件交给 finally 清理，别把整首歌留在磁盘上
                temp_audio_file = song_input_for_api
            await self._send_progress_notice(
                event,
                step=(
                    f"音源准备好了，正在提交中间层"
                    f"（{os.path.getsize(song_input_for_api) / 1024 / 1024:.1f} MiB）"
                ),
                song=selected_song.get("name", song_name),
            )

            logger.info(
                f"[CoverFlow:{flow_id}] stage=backend_connect | url={self.rvc_base_url}"
            )
            logger.info(
                f"[CoverFlow:{flow_id}] stage=backend_submit | api=/convert | "
                f"model={selected_model} | key_shift={key_shift:+d}"
            )
            __uid, __uname = self._sender_pair(event)
            self._pending_cover_meta[getattr(event, "unified_msg_origin", "")] = {
                "user": __uid, "user_name": __uname,
                "song": selected_song.get("name", song_name),
                "artist": selected_song.get("artists", ""),
                "model": selected_model}
            result_path, cache_hit = await self._predict_cover(
                event=event,
                song_name_src=song_input_for_api,
                model_name=selected_model,
                key_shift=key_shift,
            )
            logger.info(f"[CoverFlow:{flow_id}] stage=backend_done")

            if result_path and os.path.exists(result_path):
                await self._send_cover_result(
                    event, result_path, song_name=selected_song.get('name', song_name),
                    cache_hit=cache_hit, artist=selected_song.get('artists', ''),
                    model=selected_model)
                song_artist = selected_song.get('artists', '未知艺人')
                if cache_hit:
                    result_msg = f"⚡ 命中缓存！已成功使用 RVC 音色【{selected_model}】发送《{selected_song['name']}》({song_artist}) 的翻唱版本！"
                else:
                    result_msg = f"已成功使用 RVC 音色【{selected_model}】生成《{selected_song['name']}》({song_artist}) 的翻唱版本！音频文件已发送。"
                if key_shift != original_key_shift and self.enable_auto_key_shift:
                    gender_label = {"male": "男", "female": "女"}.get
                    result_msg += f"\n🎵 自动升降调：{song_artist}({gender_label(artist_gender, '?')}) → {model_display}({gender_label(model_gender, '?')})，音调 {original_key_shift:+d} → {key_shift:+d}"
            else:
                result_msg = "❌ 生成失败，后端未返回有效文件路径。"

            # === 方案E：记录偏好 ===
            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                pref["usage_count"] += 1
                pref["last_used_time"] = datetime.now().isoformat()
                pref["last_used_model"] = selected_model

                song_entry = f"{selected_song['name']} - {selected_song['artists']}"
                existing = [s for s in pref["favorite_songs"] if s.split(" - ")[0] == selected_song['name']]
                if not existing:
                    pref["favorite_songs"].append(song_entry)
                    if len(pref["favorite_songs"]) > 30:
                        pref["favorite_songs"] = pref["favorite_songs"][-30:]

                artist = selected_song['artists'].split(',')[0].strip() if ',' in selected_song['artists'] else selected_song['artists']
                artists_dict = pref.get("preferred_artists", {})
                if artist not in artists_dict:
                    artists_dict[artist] = {"count": 0, "last_time": None, "model": None}
                artists_dict[artist]["count"] += 1
                artists_dict[artist]["last_time"] = datetime.now().isoformat()
                artists_dict[artist]["model"] = selected_model
                pref["preferred_artists"] = artists_dict

                artist_model_map = pref.get("artist_model_map", {})
                if artist not in artist_model_map:
                    artist_model_map[artist] = {}
                model_key = selected_model
                if model_key not in artist_model_map[artist]:
                    artist_model_map[artist][model_key] = 0
                artist_model_map[artist][model_key] += 1
                pref["artist_model_map"] = artist_model_map

                await self._save_preferences()

            return result_msg

        except asyncio.CancelledError:
            # 只有在真正被外部取消时才会走到这里；如实记录，方便以后排查
            logger.warning(
                f"[CoverFlow:{flow_id}] 翻唱任务被取消（插件重载或上游请求中断），本次无输出"
            )
            self._finish_active_task(event, "已取消")
            raise
        except Exception as e:
            logger.error(f"[CoverFlow:{flow_id}] stage=failed\n{traceback.format_exc()}")
            if "Timeout" in str(e):
                return f"❌ 生成超时了！后端在 {self.inference_timeout} 秒内没有完成任务。"
            else:
                return f"❌ RVC 翻唱时发生错误: {e}"
        finally:
            try:
                if self.enable_send_file and result_path and os.path.isfile(result_path):
                    await asyncio.sleep(3)
                if result_path and os.path.isfile(result_path):
                    os.remove(result_path)
                if temp_audio_file and os.path.isfile(temp_audio_file):
                    os.remove(temp_audio_file)
            except OSError as cleanup_error:
                logger.debug(
                    f"删除临时文件失败（文件可能仍被占用，将在退出时清理）: {cleanup_error}"
                )


    @filter.llm_tool(name="get_available_models")
    async def get_available_models(self, event: AstrMessageEvent) -> str:
        '''获取当前可用的 RVC 音色模型列表。

        Args:
            无

        Returns:
            可用模型的详细信息列表（包含序号、显示名、文件名、别名）

        重要提示：
            - 可以直接用模型名称或别名指定模型，无需记住序号
            - 例如：model_name='塔菲' 会自动匹配到 tafeim.pth
            - 支持模糊匹配：输入部分名称也能找到
        '''
        try:
            rvc_models = self.get_models_detailed_list()
            if not rvc_models:
                return "【RVC 模型】暂无可用模型，请使用 /刷新rvc模型"

            lines = []
            for m in rvc_models:
                alias_info = f" (别名: {m['alias']})" if m['alias'] else ""
                lines.append(f"  {m['index']}. {m['display']}【{m['name']}】{alias_info}")

            result_parts = [f"【RVC 模型】(共 {len(rvc_models)} 个)\n" + "\n".join(lines)]
            result_parts.append("\n\n💡 使用方式：")
            result_parts.append("- 用序号: model_index=2 （第2个模型）")
            result_parts.append("- 用名称: model_name='tafeim' 或 model_name='塔菲' （自动匹配）")
            result_parts.append("- 示例: '用塔菲模型翻唱《晴天》'")

            status = "RVC ✅" if self.enable_rvc else "RVC ❌(已禁用)"
            result_parts.append(f"\n🔧 引擎状态: {status}")
            note = self._models_dir_note()
            if note:
                result_parts.append(note)

            return "\n".join(result_parts)
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"获取模型列表时出错: {e}"


    # ==================== 方案A：智能单步翻唱 ====================
    
    @filter.llm_tool(name="smart_cover")
    async def smart_cover(self, event: AstrMessageEvent,
                         song_name: str,
                         artist_name: Optional[str] = None,
                         model_index: Optional[int] = None,
                         model_name: Optional[str] = None,
                         key_shift: Optional[int] = None) -> str:
        '''智能一键 RVC 翻唱工具 - 自动完成搜索+选择+翻唱全流程！

        这是最高效的翻唱方式，只需提供歌曲名即可自动完成所有步骤。

        **什么时候才用这个工具（务必先判断）：**
            - 只在用户明确表达「要唱 / 唱一首 / 翻唱 / 变声 / 用某个音色唱」这类意思时才调用。
            - 用户说「点歌」「放首歌」「来一首」「听」这类是想放**原曲**，那是点歌插件的活儿，
              **绝对不要调用本工具**，否则会把听歌变成翻唱。
            - 拿不准是「听原曲」还是「翻唱」时，先问一句「你要原曲还是唱一遍？」再决定。

        Args:
            song_name(string): 用户说的歌名，**原样整串**填进来，不要拆分。
                - 「唱雨爱司凤版」→ song_name='雨爱司凤版'，artist_name 不填
                - 「唱周杰伦的晴天」→ song_name='晴天'，artist_name='周杰伦'
                版本名/别名（司凤版、伴奏版、纯享版、DJ版…）属于歌名的一部分，
                必须留在 song_name 里，**不要**塞进 artist_name。
            artist_name(string, optional): 只有用户**明确说出歌手**时才填
                （如「周杰伦的晴天」「晴天 周杰伦 唱的」「歌手是周杰伦」）。
                用户没说歌手就**绝对不要填**，不许凭印象或搜索结果猜。
            model_index(number, optional): 模型序号（从1开始）。不指定则使用默认值或用户偏好
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。优先级高于model_index
                例如：'塔菲'会自动匹配到tafeim.pth，'maxmaxpoi'也能匹配
            key_shift(number, optional): 音调调整，范围 -12 到 12；不指定则使用默认值或用户偏好。
                调用此工具前，你应该分析原曲演唱者性别与目标模型音域的匹配关系（如周杰伦男声→塔菲喵女声）。但分析完成后请不要传入 key_shift，插件会根据分析结果自动应用配置中的升降调值（男→女用 male_to_female_shift，女→男用 female_to_male_shift）。只有当用户明确要求特定数值（如"升3调"）时才传入 key_shift。

        Returns:
            翻唱结果描述（异步模式会立即返回"正在处理"）

        注意：
            - model_name 支持中文别名和文件名，无需记住序号！
            - 如果同时指定 model_index 和 model_name，以 model_name 为准
            - 本工具采用异步执行，会立即返回并后台处理，完成后自动发送音频
            - 取歌走插件配置的音源顺序（默认网易云 → 酷狗 → 酷我 → QQ 音乐）
        '''
        rejected = self._reject_music_request(event, "smart_cover")
        if rejected:
            return rejected
        if not self.enable_smart_cover:
            return "智能单步翻唱功能已在配置中禁用。请使用对应的独立翻唱工具。"

        try:
            if not self.enable_rvc:
                return "❌ RVC 功能已在配置中禁用，请先在插件配置里开启 enable_rvc。"

            # === 方案B：进行中检查已移除（CUSTOM-8.1：新任务由统一队列排队） ===

            if model_name:
                matched_index = self._find_model_index_by_name(model_name)
                if matched_index:
                    actual_model_index = matched_index
                else:
                    available = await self.get_available_models(event)
                    return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用的模型：\n{available}\n\n💡 提示：可以使用 get_available_models 查看完整列表"
            else:
                actual_model_index = model_index or self.fallback_model_index

            actual_key_shift = (
                key_shift if key_shift is not None else self.default_key_shift
            )

            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)

                if not model_name and not model_index and pref.get("model_index"):
                    actual_model_index = pref["model_index"]

                if key_shift is None:
                    actual_key_shift = pref.get("default_key_shift", self.default_key_shift)

                if artist_name and not model_name and not model_index:
                    artist_model_map = pref.get("artist_model_map", {})
                    if artist_name in artist_model_map:
                        model_counts = artist_model_map[artist_name]
                        if model_counts:
                            best_model = max(model_counts, key=model_counts.get)
                            models_info_tmp = self.get_models_detailed_list()
                            for m in models_info_tmp:
                                if m["filename"] == best_model or m["display"] == best_model:
                                    actual_model_index = m["index"]
                                    break

            # 配置下拉选的默认音色：用户没显式指定模型名/序号时优先用它
            if not model_name and not model_index:
                configured_default_index = self._configured_rvc_model_index()
                if configured_default_index:
                    actual_model_index = configured_default_index

            models_info = self.get_models_detailed_list()
            actual_model_display = ""
            artist_pref_hint = ""
            for m in models_info:
                if m["index"] == actual_model_index:
                    actual_model_display = m["display"]
                    break

            if self.enable_preference_learning and artist_name and not model_name and not model_index:
                artist_model_map = pref.get("artist_model_map", {})
                if artist_name in artist_model_map:
                    model_counts = artist_model_map[artist_name]
                    best_model = max(model_counts, key=model_counts.get)
                    for m in models_info:
                        if (m["filename"] == best_model or m["display"] == best_model) and m["index"] == actual_model_index:
                            artist_pref_hint = f"，这次按老习惯挑了翻唱{artist_name}常用的音色"
                            break

            # 注册活跃任务
            if not self._register_active_task(event, song_name, actual_model_display):
                return self._last_register_reject_reason or "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"

            task = self._create_tracked_task(self._smart_cover_async(
                event, song_name, actual_model_index,
                actual_key_shift, actual_model_display,
                artist_name=artist_name,
            ))
            self._bind_active_asyncio_task(event, task)

            # 只回一句中性短句给 LLM，具体怎么说由人格模型组织；不再拼符号和参数
            return f"好，我去弄这首《{song_name}》，好了直接发你。{artist_pref_hint}（系统会直接把结果发到群里，失败也会另行通知；这条只是受理确认，不要向用户复述、转述或展开它。）"

        except Exception as e:
            logger.error(traceback.format_exc())
            return f"智能翻唱出错: {e}"


    async def _smart_cover_async(self, event: AstrMessageEvent, song_name: str,
                                 model_index: int, key_shift: Optional[int],
                                 model_display: str,
                                 artist_name: str = ""):
        """smart_cover 异步执行函数。

        ``song_name`` 是用户说的歌名整串（版本名留在里面），``artist_name`` 只在
        用户明确说出歌手时才有值，两者分开传给取歌入口，不再拼成一个搜索串。
        """
        try:
            logger.info(
                f"[任务Debug] 后台翻唱流程已进入 | song={song_name} | "
                f"artist={artist_name or '-'} | model={model_display}"
            )
            result = await self._do_cover(
                event, song_name, model_index, key_shift,
                artist_name=artist_name,
            ) or ""

            # 失败判断只认明确标记：❌ 开头，或 FAIL: 开头（保留 ❌ 兼容 _do_cover 现有返回点）。
            # 不用关键词猜（"失败"/"错误"/"超时"…）：正常文案里带"失败"两个字就会被误判成任务失败。
            # _do_cover 的返回值协议本次不动。
            is_failed = result.startswith("❌") or result.startswith("FAIL:")

            # === 失败：技术细节只进日志，用户只听到一句话 ===
            if is_failed:
                logger.warning(
                    f"[任务Debug] 翻唱失败 | song={song_name} | "
                    f"model={model_display} | detail={result}"
                )
                await self.outbound.say(
                    event, "failed", song=song_name, model=model_display, reason=result
                )
            # === 成功提示：模板文案 / LLM 播报是两个独立开关（CUSTOM-5.1）===
            cache_hit = "命中缓存" in result or "cache hit" in result.lower()
            if not is_failed and self.enable_config_report:
                report_shift = int(key_shift or 0)
                result += f"\n\n📊 本次配置：模型={model_display}, 调音={report_shift:+d}"
                await event.send(event.plain_result(result))
            if not is_failed and self.enable_llm_success_notify:
                await self._notify_llm_with_context(
                    event, song_name, "cache_hit" if cache_hit else "success")

        except Exception as e:
            logger.error(f"smart_cover 异步执行失败: {traceback.format_exc()}")
            try:
                # 异常也走统一出口：只说一句人话，异常栈留在日志里
                await self.outbound.say(
                    event, "failed", song=song_name, model=model_display, reason=str(e)
                )
            except Exception:
                pass

        finally:
            # 方案B：无论成功失败，都清理活跃任务
            self._finish_active_task(event)


    # ==================== 方案C：确认机制 ====================
    
    @filter.llm_tool(name="confirm_selection")
    async def confirm_selection(self, event: AstrMessageEvent,
                               selection_type: str,
                               selection_data: str,
                               action: str = "confirm") -> str:
        '''确认或修改用户的 selections - 支持自然语言交互！

        当用户说'就这个''换一个''确定''取消'等自然语言时使用此工具。
        
        Args:
            selection_type(string): 选择类型，'song'(歌曲)、'model'(模型)、'config'(配置)
            selection_data(string): 当前选择的描述信息，如'歌曲：《晴天》-周杰伦'或'模型：第3个-RVC'
            action(string): 用户意图，'confirm'(确认)、'change'(更换)、'cancel'(取消)、'info'(查看详情)
        
        Returns:
            确认结果和下一步建议
        '''
        if not self.enable_confirm_mechanism:
            return "确认机制已在配置中禁用。"
        
        try:
            responses = {
                "confirm": {
                    "song": f"✅ 已确认选择：{selection_data}\n接下来将执行翻唱操作。",
                    "model": f"✅ 已确认使用：{selection_data}",
                    "config": f"✅ 配置已确认：{selection_data}\n准备开始翻唱..."
                },
                "change": {
                    "song": f"🔄 需要更换歌曲选择。\n当前选择：{selection_data}\n请重新搜索或指定新的歌曲。",
                    "model": f"🔄 需要更换模型。\n当前选择：{selection_data}\n可用模型列表：\n{(await self.get_available_models(event))}",
                    "config": f"🔄 需要修改配置。\n当前配置：{selection_data}\n请告诉我新的参数值。"
                },
                "cancel": {
                    "song": "❌ 已取消歌曲选择。",
                    "model": "❌ 已取消模型选择。",
                    "config": "❌ 已取消配置，操作终止。"
                },
                "info": {
                    "song": f"📋 歌曲详情：{selection_data}",
                    "model": f"📋 模型详情：{selection_data}",
                    "config": f"⚙️ 当前配置详情：{selection_data}"
                }
            }
            
            action_lower = action.lower() if action else "confirm"
            type_lower = selection_type.lower() if selection_type else "song"
            
            if action_lower not in responses:
                return f"未知操作: {action}。支持的选项: confirm, change, cancel, info"
            
            if type_lower not in responses[action_lower]:
                return f"未知选择类型: {selection_type}。支持的类型: song, model, config"
            
            result = responses[action_lower][type_lower]
            
            if action_lower == "confirm" and type_lower == "song":
                result += "\n\n💡 提示：您也可以直接说'就用这个翻唱'，我会自动继续！"
            
            return result
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"确认操作出错: {e}"

    # ==================== 方案D：批量翻唱 ====================
    
    @filter.llm_tool(name="batch_cover")
    async def batch_cover(self, event: AstrMessageEvent,
                         songs: List[str],
                         model_index: Optional[int] = None,
                         key_shift: Optional[int] = None) -> str:
        '''批量 RVC 翻唱多首歌曲 - 异步后台执行，支持长时间任务！

        适合说'帮我翻唱这几首歌：《A》《B》《C》'的场景。

        重要特性：
            - 异步执行：立即返回确认消息，后台处理翻唱
            - 实时推送：每完成一首歌立即发送音频文件
            - 超时安全：不受60秒工具超时限制

        Args:
            songs(list[string]): 要翻唱的歌曲名称列表（必填）
            model_index(number, optional): 统一使用的模型序号
            key_shift(number, optional): 统一音调值，范围 -12 到 12。
                调用此工具前，你应该分析原曲演唱者性别与目标模型音域的匹配关系（如周杰伦男声→塔菲喵女声）。但分析完成后请不要传入 key_shift，插件会根据分析结果自动应用配置中的升降调值（男→女用 male_to_female_shift，女→男用 female_to_male_shift）。只有当用户明确要求特定数值（如"升3调"）时才传入 key_shift。

        Returns:
            任务确认信息（实际结果会异步推送到聊天中）
        '''
        rejected = self._reject_music_request(event, "batch_cover")
        if rejected:
            return rejected
        if not self.enable_batch_cover:
            return "批量翻唱功能已在配置中禁用，请逐个使用 smart_cover。"

        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用，请先在插件配置里开启 enable_rvc。"

        # === 方案B：进行中检查已移除（CUSTOM-8.1：新任务由统一队列排队） ===

        try:
            if not songs or len(songs) == 0:
                return "歌曲列表为空，请提供至少一首歌名。"

            if len(songs) > self.max_batch_size:
                return f"批量翻唱数量超过限制（最大{self.max_batch_size}首）。当前提供了{len(songs)}首，请分批处理。"

            # === 参数解析 ===
            actual_model_index = model_index or self.fallback_model_index
            actual_key_shift = (
                key_shift if key_shift is not None else self.default_key_shift
            )

            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                if not model_index:
                    actual_model_index = pref.get("model_index", actual_model_index)
                if key_shift is None:
                    actual_key_shift = pref.get("default_key_shift", actual_key_shift)

            # 配置下拉选的默认音色：用户没显式指定序号时优先用它
            if not model_index:
                configured_default_index = self._configured_rvc_model_index()
                if configured_default_index:
                    actual_model_index = configured_default_index

            # 注册活跃任务
            batch_song_names = "、".join(songs[:3])
            if len(songs) > 3:
                batch_song_names += f" 等{len(songs)}首"
            if not self._register_active_task(event, f"[批量]{batch_song_names}"):
                return self._last_register_reject_reason or "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"

            # === 创建后台任务（异步执行）===
            task = self._create_tracked_task(
                self._execute_batch_cover_async(
                    event=event,
                    songs=songs,
                    model_index=actual_model_index,
                    key_shift=actual_key_shift,
                )
            )
            self._bind_active_asyncio_task(event, task)

            # === 立即返回确认消息（不会被超时取消）===
            report_shift = int(actual_key_shift or 0)
            confirm_msg = (
                f"📦 已接收批量翻唱任务！共 {len(songs)} 首歌曲\n"
                f"⚙️ 配置：RVC / 第{actual_model_index}个模型 / 音调{report_shift:+d}\n"
                f"⏳ 正在后台处理中，请稍候...\n"
                f"💡 每完成一首会自动发送音频，最后发送汇总报告"
            )

            return confirm_msg

        except Exception as e:
            logger.error(traceback.format_exc())
            return f"批量翻唱启动失败: {e}"


    async def _execute_batch_cover_async(self, event: AstrMessageEvent,
                                        songs: List[str],
                                        model_index: int,
                                        key_shift: Optional[int]):
        """异步执行批量翻唱（后台任务）"""
        try:
            results = []
            success_count = 0
            fail_count = 0

            for idx, song_name in enumerate(songs, 1):
                try:
                    # 进度提示走统一出口（默认静音，由 notify_progress 控制）
                    await self.outbound.say(
                        event, "progress", song=song_name, count=idx, total=len(songs)
                    )

                    # 执行翻唱
                    result = await self._do_cover(event, song_name, model_index, key_shift) or ""
                    # 与 _smart_cover_async 保持一致：只认明确的失败标记，不用关键词猜
                    is_failed = result.startswith("❌") or result.startswith("FAIL:")
                    if is_failed:
                        # 报告只留一句人话，技术细节（含中间层原始报错）只写日志
                        logger.warning(
                            f"批量翻唱第{idx}首失败: {song_name} | detail={result}"
                        )
                        results.append(
                            f"❌ [{idx}/{len(songs)}] 《{song_name}》："
                            f"{self._short_failure_reason(result)}"
                        )
                        fail_count += 1
                    else:
                        results.append(f"✅ [{idx}/{len(songs)}] 《{song_name}》：成功")
                        success_count += 1

                    # 歌间延迟避免过快请求
                    if idx < len(songs):
                        await asyncio.sleep(2)

                except asyncio.CancelledError:
                    results.append(f"❌ [{idx}/{len(songs)}] 《{song_name}》：被取消")
                    fail_count += 1
                    logger.warning(f"批量翻唱第{idx}首被取消")
                except Exception as e:
                    logger.error(f"批量翻唱第{idx}首出错: {e}", exc_info=True)
                    results.append(
                        f"❌ [{idx}/{len(songs)}] 《{song_name}》：这首没做成"
                    )
                    fail_count += 1

            # === 生成并发送汇总报告 ===
            summary_lines = [
                "",
                "=" * 50,
                "📊 批量翻唱任务完成报告",
                "=" * 50,
                "",
                f"总任务数: {len(songs)} 首",
                f"✅ 成功: {success_count} 首",
                f"❌ 失败: {fail_count} 首",
                "",
                f"使用的配置:",
                f"  引擎: RVC",
                f"  模型: 第{model_index}个",
                f"  音调: {int(key_shift or 0):+d}",
                "",
                "-" * 50,
                "详细结果:",
            ]

            summary_lines.extend(results)

            summary_lines.extend([
                "",
                "=" * 50,
                f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "=" * 50
            ])

            final_report = "\n".join(summary_lines)

            # 发送汇总报告
            try:
                await event.send(event.plain_result(final_report))
            except Exception as e:
                logger.error(f"发送批量翻唱报告失败: {e}")

            # === LLM 成功通知机制 ===
            if getattr(self, "enable_llm_success_notify", True) and success_count > 0:
                try:
                    provider = self.context.get_using_provider(event.unified_msg_origin)
                    if provider:
                        prompt = f"系统通知：刚才用户要求的批量翻唱任务已经处理完成！共成功处理了 {success_count} 首歌曲。请用你当前的人设直接告诉用户这个好消息（不要包含系统通知字样）。"
                        llm_resp = await provider.text_chat(prompt=prompt)
                        if llm_resp and llm_resp.completion_text:
                            await event.send(event.plain_result(llm_resp.completion_text))
                except Exception as llm_e:
                    logger.error(f"通知 LLM 失败: {llm_e}")

        except Exception as e:
            logger.error(traceback.format_exc())
            # 技术细节只进日志，报告里只说一句人话
            error_report = "❌ 批量翻唱没跑完，具体原因已经记到日志里了"
            try:
                await event.send(event.plain_result(error_report))
            except Exception:
                pass
        finally:
            # 方案B：批量任务结束后清理活跃任务
            self._finish_active_task(event)


    # ==================== 方案E：偏好学习与推荐系统 ====================
    
    @filter.llm_tool(name="save_preference")
    async def save_preference(self, event: AstrMessageEvent,
                             preference_type: str,
                             value: str) -> str:
        '''保存用户的偏好设置 - 让AI记住你的喜好！

        可以保存常用的模型、音调等设置，下次翻唱时自动应用。

        Args:
            preference_type(string): 偏好类型：
                - 'model_index': 默认模型序号 (数字字符串)
                - 'key_shift': 默认音调调整（-12 到 12）
            value(string): 偏好值（统一使用字符串传入，系统会自动转换）

        Returns:
            保存结果确认
        '''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。"

        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)

            valid_types = {
                "model_index": {"type": int, "min": 1},
                "key_shift": {"type": int, "min": -12, "max": 12}
            }

            if preference_type not in valid_types:
                return f"无效的偏好类型: {preference_type}\n支持的类型: {', '.join(valid_types.keys())}"

            type_info = valid_types[preference_type]
            target_type = type_info["type"]

            try:
                converted_value = target_type(value)
            except (ValueError, TypeError):
                return f"无法将值 '{value}' 转换为 {target_type.__name__} 类型"

            if "valid_values" in type_info and converted_value not in type_info["valid_values"]:
                return f"无效的值: {converted_value}，可选值: {type_info['valid_values']}"

            if "min" in type_info and converted_value < type_info["min"]:
                return f"值太小: {converted_value}，最小值为 {type_info['min']}"

            if "max" in type_info and converted_value > type_info["max"]:
                return f"值太大: {converted_value}，最大值为 {type_info['max']}"

            pref[f"default_{preference_type}"] = converted_value
            await self._save_preferences()

            type_display = {
                "model_index": f"默认模型 → 第{converted_value}个",
                "key_shift": f"默认音调 → {converted_value:+d}"
            }

            return f"✅ 偏好已保存！\n{type_display.get(preference_type, preference_type)}\n\n下次翻唱时会自动使用此设置。如需修改，随时告诉我！"

        except Exception as e:
            logger.error(traceback.format_exc())
            return f"保存偏好时出错: {e}"


    @filter.llm_tool(name="get_recommendation")
    async def get_recommendation(self, event: AstrMessageEvent,
                                song_name: Optional[str] = None) -> str:
        '''基于历史记录智能推荐最佳翻唱配置！

        分析用户的历史使用习惯，推荐最合适的模型和参数。
        
        Args:
            song_name(string, optional): 计划翻唱的歌曲名（可选，用于结合歌曲特征推荐）
        
        Returns:
            推荐的配置方案和使用理由
        '''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无法提供个性化推荐。"
        
        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)
            
            recommendation = []
            recommendation.append("🤖 基于您的使用历史，我为您推荐以下配置：\n")
            
            # === 基本信息 ===
            usage_count = pref.get("usage_count", 0)
            last_time = pref.get("last_used_time", "无")
            
            recommendation.append(f"📊 您的使用统计：")
            recommendation.append(f"   - 总翻唱次数: {usage_count}")
            recommendation.append(f"   - 最后使用时间: {last_time[:16] if last_time != '无' else last_time}")
            
            if usage_count == 0:
                recommendation.append(f"\n💡 您还没有使用过翻唱功能，将使用系统默认配置：")
                recommendation.append(f"   - 模型: 第{self.fallback_model_index}个")
                recommendation.append(f"   - 音调: {self.default_key_shift:+d}")
                return "\n".join(recommendation)

            # === 推荐配置 ===
            rec_model_index = pref.get("model_index", self.fallback_model_index)
            rec_key_shift = pref.get("default_key_shift", self.default_key_shift)
            last_model = pref.get("last_used_model", "未知")
            
            recommendation.append(f"\n⭐ 推荐配置：")
            recommendation.append(f"   🎤 模型选择: 第{rec_model_index}个 ({last_model})")
            recommendation.append(f"   🎼 音调调整: {rec_key_shift:+d}")
            
            # === 收藏歌曲 ===
            favorite_songs = pref.get("favorite_songs", [])
            if favorite_songs:
                recent_favorites = favorite_songs[-5:] if len(favorite_songs) > 5 else favorite_songs
                recommendation.append(f"\n❤️ 您最近常听的歌曲：")
                for song in recent_favorites:
                    recommendation.append(f"   - 《{song}》")
            
            # === 偏好歌手（按频率排序）===
            preferred_artists = pref.get("preferred_artists", {})
            if preferred_artists:
                sorted_artists = sorted(preferred_artists.items(), key=lambda x: x[1].get("count", 0), reverse=True)
                recommendation.append(f"\n🎤 您偏爱的歌手（按频率排序）：")
                for artist, info in sorted_artists[:5]:
                    model_info = f", 常用模型: {info.get('model', '未知')}" if info.get('model') else ""
                    recommendation.append(f"   - {artist} ({info.get('count', 0)}次{model_info})")
            
            # === 歌手→模型映射 ===
            artist_model_map = pref.get("artist_model_map", {})
            if artist_model_map:
                recommendation.append(f"\n🔗 歌手专属模型映射：")
                for artist, models in sorted(artist_model_map.items()):
                    best = max(models, key=models.get)
                    recommendation.append(f"   - {artist} → {best} ({models[best]}次)")
            
            # === 结合当前歌曲的建议 ===
            if song_name:
                recommendation.append(f"\n🎯 针对《{song_name}》的建议：")
                
                matched_artist = None
                for artist in preferred_artists:
                    if artist and artist in song_name:
                        matched_artist = artist
                        break
                
                if matched_artist:
                    recommendation.append(f"   ✨ 检测到这是您偏爱的歌手 {matched_artist} 的作品！")
                    if matched_artist in artist_model_map:
                        best_model = max(artist_model_map[matched_artist], key=artist_model_map[matched_artist].get)
                        recommendation.append(f"   💡 建议使用模型 {best_model}（您翻唱此歌手时最常用）")
                    else:
                        recommendation.append(f"   💡 建议使用您常用的配置进行翻唱。")
                
                song_names = [s.split(" - ")[0] for s in favorite_songs]
                if song_name in song_names:
                    recommendation.append(f"   🔄 这首歌您之前翻唱过！")
                    recommendation.append(f"   💡 是否想尝试不同的模型或音调？")
            
            # === 使用建议 ===
            recommendation.append(f"\n💬 如何使用此推荐？")
            recommendation.append(f"   直接说：'翻唱《{song_name or '歌曲名'}》'")
            recommendation.append(f"   我会自动应用以上推荐的配置！")
            recommendation.append(f"\n⚙️ 如需修改偏好，可以说：")
            recommendation.append(f"   - '把默认模型改成第2个'")
            recommendation.append(f"   - '默认升2调'")
            
            return "\n".join(recommendation)
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"获取推荐时出错: {e}"

    @filter.llm_tool(name="view_my_stats")
    async def view_my_stats(self, event: AstrMessageEvent) -> str:
        '''查看个人使用统计和历史记录。

        显示您的翻唱历史、常用配置、收藏歌曲等信息。
        
        Returns:
            个人统计信息的详细报告
        '''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无法查看统计数据。"
        
        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)
            
            stats = []
            stats.append("=" * 60)
            stats.append("📈 个人翻唱统计报告")
            stats.append("=" * 60)
            
            # === 基础统计 ===
            usage_count = pref.get("usage_count", 0)
            last_time = pref.get("last_used_time", "从未使用")
            
            stats.append(f"\n🔢 基础数据：")
            stats.append(f"   总翻唱次数: {usage_count}")
            stats.append(f"   最后使用: {last_time[:19] if last_time != '从未使用' else last_time}")
            
            # === 当前偏好 ===
            stats.append(f"\n⚙️ 当前偏好设置：")
            stats.append(f"   默认模型: 第{pref.get('model_index', '未设置')}个")
            stats.append(f"   默认音调: {pref.get('default_key_shift', '未设置'):+d}" if isinstance(pref.get('default_key_shift'), int) else "   默认音调: 未设置")
            
            # === 上次使用 ===
            last_model = pref.get("last_used_model", "无")
            stats.append(f"\n🎯 上次使用：")
            stats.append(f"   模型: {last_model}")
            
            # === 收藏歌曲 ===
            favorite_songs = pref.get("favorite_songs", [])
            if favorite_songs:
                stats.append(f"\n❤️ 收藏歌曲 ({len(favorite_songs)}首)：")
                for i, song in enumerate(favorite_songs[-10:], 1):
                    stats.append(f"   {i}. 《{song}》")
            else:
                stats.append(f"\n❤️ 收藏歌曲：暂无")
            
            # === 偏爱歌手 ===
            preferred_artists = pref.get("preferred_artists", {})
            if preferred_artists:
                sorted_artists = sorted(preferred_artists.items(), key=lambda x: x[1].get("count", 0), reverse=True)
                stats.append(f"\n🎤 偏爱歌手（按频率排序）：")
                for artist, info in sorted_artists[:10]:
                    model_info = f", 常用: {info.get('model', '未知')}" if info.get('model') else ""
                    stats.append(f"   • {artist} - {info.get('count', 0)}次{model_info}")
            else:
                stats.append(f"\n🎤 偏爱歌手：暂无")
            
            # === 歌手→模型映射 ===
            artist_model_map = pref.get("artist_model_map", {})
            if artist_model_map:
                stats.append(f"\n🔗 歌手专属模型映射：")
                for artist, models in sorted(artist_model_map.items()):
                    best = max(models, key=models.get)
                    stats.append(f"   • {artist} → {best} ({models[best]}次)")
            
            # === 操作建议 ===
            stats.append(f"\n" + "-" * 60)
            stats.append(f"💡 可用操作：")
            stats.append(f"   • 查看推荐配置：'给我推荐个配置'")
            stats.append(f"   • 修改偏好设置：'把默认模型改成第2个'")
            stats.append(f"   • 清空历史数据：'清空我的翻唱历史'")
            
            stats.append(f"\n" + "=" * 60)
            stats.append(f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            stats.append("=" * 60)
            
            return "\n".join(stats)
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"查看统计时出错: {e}"

    @filter.llm_tool(name="clear_my_history")
    async def clear_my_history(self, event: AstrMessageEvent) -> str:
        '''清空个人的翻唱历史和偏好数据。

        删除所有与当前用户相关的使用记录、偏好设置、收藏等信息。
        
        Returns:
            清除操作的结果确认
        '''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无需清除数据。"
        
        try:
            user_id = self._get_user_id(event)
            
            if user_id not in self.user_preferences:
                return "没有找到您的任何历史数据。"
            
            del self.user_preferences[user_id]
            await self._save_preferences()
            
            return "✅ 已清除您的所有翻唱历史和偏好数据！\n\n包括：\n• 使用统计\n• 偏好设置\n• 收藏歌曲\n• 偏爱歌手记录\n\n下次使用时将从零开始重新学习您的喜好。"
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"清除历史时出错: {e}"

    # ==================== 本地音频翻唱 LLM 工具 ====================
    
    async def _resolve_local_audio_shift(
        self,
        audio_path: str,
        model_display: str,
        key_shift: Optional[int],
        event: AstrMessageEvent,
    ) -> tuple[int, Optional[int], str]:
        """本地音频的升降调：LLM 传入 → LLM 推断 → 本地推断 → 默认配置。

        Returns:
            ``(最终音调, 推断出来的音调或 None, 来源说明)``。
        """
        if key_shift is not None:
            return key_shift, None, "LLM传入"

        if self.enable_auto_key_shift:
            filename_base = os.path.splitext(os.path.basename(audio_path))[0]
            # 清理常见后缀：_EM, _cover, _RVC, (Live), [HQ] 等
            song_name = re.sub(r'[_\-\s]*(?:cover|rvc|em|ai|remix|inst|伴奏|翻唱|完整版|live|现场|mv|hq|flac|mp3|wav| instrumental)?\s*$', '', filename_base, flags=re.I).strip()
            song_gender, model_gender = await self._detect_gender_pair(
                song_name, model_display, event, source_is_song=True
            )
            if song_gender and model_gender:
                shifted = await self._calc_auto_key_shift(
                    song_name, model_display, 0, song_gender, model_gender, event
                )
                logger.info(f"[RVC控制台] LLM自动推断音调: key_shift={shifted} (歌曲: {song_name}, 模型: {model_display})")
                return shifted, shifted, "LLM推断"

        inferred = self._infer_key_shift_from_filename(audio_path, model_display)
        if inferred is not None:
            logger.info(f"[RVC控制台] 本地推断音调: key_shift={inferred} (文件: {os.path.basename(audio_path)}, 模型: {model_display})")
            return inferred, inferred, "本地推断"
        return self.default_key_shift, None, "默认配置"

    @filter.llm_tool(name="cover_absolute_path_audio")
    async def cover_absolute_path_audio(self, event: AstrMessageEvent,
                                         audio_path: str,
                                         model_name: Optional[str] = None,
                                         model_index: Optional[int] = None,
                                         key_shift: Optional[int] = None) -> str:
        '''将指定绝对路径的音频文件进行 RVC 翻唱/音色转换。

        当配合其他插件（如 ide_sandbox）获取到了本地文件的绝对路径，并且用户要求对其进行翻唱时使用此工具。
        注意：只能处理本机确实存在的音频文件路径。

        【关于音调调整】
        如果你不确定应该升调还是降调，请不要传入 key_shift 参数，插件会根据歌曲和模型的匹配关系自动推断最佳音调。
        只有当用户明确要求特定音调（如"升3调"、"降2调"）时，才需要传入 key_shift。

        Args:
            audio_path(string): 必须是音频文件的完整绝对路径（如 C:\\path\\to\\audio.mp3）
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。不指定则使用默认模型
            model_index(number, optional): 模型序号（从1开始）。不指定则使用默认模型
            key_shift(number, optional): 音调调整，范围 -12 到 12；不指定则使用默认配置。
                调用此工具前，你应该分析原曲演唱者性别与目标模型音域的匹配关系（如周杰伦男声→塔菲喵女声）。但分析完成后请不要传入 key_shift，插件会根据分析结果自动应用配置中的升降调值（男→女用 male_to_female_shift，女→男用 female_to_male_shift）。只有当用户明确要求特定数值（如"升3调"）时才传入 key_shift。

        Returns:
            处理结果的描述信息（异步执行，完成后自动发送音频）
        '''
        rejected = self._reject_music_request(event, "cover_absolute_path_audio")
        if rejected:
            return rejected
        if not self.enable_local_audio_cover:
            return "❌ 本地音频翻唱功能未启用。"

        if not audio_path or not os.path.exists(audio_path) or not os.path.isfile(audio_path):
            return f"❌ 提供的文件路径无效或文件不存在: {audio_path}"

        if not self._is_audio_file(audio_path):
            return f"❌ 提供的文件不是受支持的音频格式: {audio_path}"

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > self.max_local_audio_size_mb:
            return f"❌ 音频文件过大 ({size_mb:.1f}MB)，超过限制 {self.max_local_audio_size_mb}MB。"

        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用。"

        actual_model_index = model_index
        actual_model_display = ""
        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if matched:
                actual_model_index = matched
            else:
                available = await self.get_available_models(event)
                return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用模型:\n{available}"

        # 没显式指定序号时，优先用配置下拉选的默认 RVC 音色
        if actual_model_index is None:
            actual_model_index = self._configured_rvc_model_index() or 1

        models_info = self.get_models_detailed_list()
        for m in models_info:
            if m["index"] == actual_model_index:
                actual_model_display = m["display"]
                break

        actual_key_shift, inferred_key_shift, inference_source = (
            await self._resolve_local_audio_shift(
                audio_path, actual_model_display, key_shift, event
            )
        )

        self._create_tracked_task(self._execute_local_cover(
            event, audio_path,
            model_name=None, model_index=actual_model_index,
            key_shift=actual_key_shift
        ))

        shift_info = f" | 升降调: {int(actual_key_shift or 0):+d}"
        if inferred_key_shift is not None:
            shift_info += f" ({inference_source})"
        return f"🎵 正在用【{actual_model_display}】(RVC) 处理文件 {os.path.basename(audio_path)}{shift_info}... 请稍候，处理完成后会自动发送！"


    @filter.llm_tool(name="cover_local_audio")
    async def cover_local_audio(self, event: AstrMessageEvent,
                                 model_name: Optional[str] = None,
                                 model_index: Optional[int] = None,
                                 key_shift: Optional[int] = None) -> str:
        '''将用户发送的本地音频文件或语音消息进行 RVC 翻唱/音色转换。

        当用户发送了语音或音频文件并要求"翻唱这个"、"把我的声音变成塔菲"、"修音"时使用此工具。

        【关于音调调整】
        如果你不确定应该升调还是降调，请不要传入 key_shift 参数，插件会根据歌曲和模型的匹配关系自动推断最佳音调。
        只有当用户明确要求特定音调（如"升3调"、"降2调"）时，才需要传入 key_shift。

        Args:
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。不指定则使用默认模型
            model_index(number, optional): 模型序号（从1开始）。不指定则使用默认模型
            key_shift(number, optional): 音调调整，范围 -12 到 12；不指定则使用默认配置。
                调用此工具前，你应该分析原曲演唱者性别与目标模型音域的匹配关系（如周杰伦男声→塔菲喵女声）。但分析完成后请不要传入 key_shift，插件会根据分析结果自动应用配置中的升降调值（男→女用 male_to_female_shift，女→男用 female_to_male_shift）。只有当用户明确要求特定数值（如"升3调"）时才传入 key_shift。

        Returns:
            处理结果的描述信息（异步执行，完成后自动发送音频）
        '''
        rejected = self._reject_music_request(event, "cover_local_audio")
        if rejected:
            return rejected
        if not self.enable_local_audio_cover:
            return "❌ 本地音频翻唱功能未启用。"

        audio_path = await self._extract_audio_from_event(event)

        if not audio_path:
            await self._cleanup_expired_audio_cache()
            cached = self._recent_audio_cache.get(str(event.unified_msg_origin))
            if cached and os.path.exists(cached[0]):
                audio_path = cached[0]
            else:
                return "❌ 未检测到音频文件或语音消息。请用户先发送要处理的音频文件或语音，然后再调用此工具。"

        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用。"

        actual_model_index = model_index
        actual_model_display = ""
        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if matched:
                actual_model_index = matched
            else:
                available = await self.get_available_models(event)
                return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用模型:\n{available}"

        # 没显式指定序号时，优先用配置下拉选的默认 RVC 音色
        if actual_model_index is None:
            actual_model_index = self._configured_rvc_model_index() or 1

        models_info = self.get_models_detailed_list()
        for m in models_info:
            if m["index"] == actual_model_index:
                actual_model_display = m["display"]
                break

        actual_key_shift, inferred_key_shift, inference_source = (
            await self._resolve_local_audio_shift(
                audio_path, actual_model_display, key_shift, event
            )
        )

        self._create_tracked_task(self._execute_local_cover(
            event, audio_path,
            model_name=None, model_index=actual_model_index,
            key_shift=actual_key_shift
        ))

        shift_info = f" | 升降调: {int(actual_key_shift or 0):+d}"
        if inferred_key_shift is not None:
            shift_info += f" ({inference_source})"
        return f"🎵 正在用【{actual_model_display}】(RVC) 处理您的音频{shift_info}... 请稍候，处理完成后会自动发送！"


    # ==================== 方案A+B：任务状态查询与取消 ====================
    
    @filter.llm_tool(name="get_task_status")
    async def get_task_status(self, event: AstrMessageEvent) -> str:
        '''查询当前是否有正在进行的翻唱任务。

        当用户问"我的翻唱好了吗"、"任务进行得怎么样了"或"有没有在翻唱"时使用此工具。
        
        Returns:
            当前翻唱任务的状态信息
        '''
        if not self.enable_task_tracking:
            return "任务追踪功能已在配置中禁用。"
        
        active_task = self._get_active_task_status(event)
        if not active_task:
            return "✅ 当前没有正在进行的翻唱任务nya～您可以随时发起新的翻唱请求喵！🐾"
        
        elapsed = int(time.time() - active_task.get("start_time", 0))
        minutes, seconds = divmod(elapsed, 60)
        time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"
        
        return (
            f"⏳ 有翻唱任务正在进行中nya～\n"
            f"歌曲：《{active_task['song_name']}》\n"
            f"模型：{active_task.get('model_display', '未知')}\n"
            f"状态：{active_task['status']}\n"
            f"已进行：{time_str}\n"
            f"请稍等，完成后会自动发送音频喵！如果卡住了可以用 cancel_cover_task 取消🐾"
        )

    @filter.llm_tool(name="cancel_cover_task")
    async def cancel_cover_task(self, event: AstrMessageEvent) -> str:
        '''取消当前正在进行的翻唱任务。

        当用户说"取消翻唱"、"不唱了"、"停下来"或任务卡住太久时使用此工具。
        
        Returns:
            取消操作的结果
        '''
        if not self.enable_task_tracking:
            return "任务追踪功能已在配置中禁用。"

        lines = []
        uid = self._get_user_id(event)
        # CUSTOM-8.1：取消该用户所有进行中的任务（翻唱+TTS，各自携带句柄）
        stopped = []
        for rec in self.tasks.user_tasks(uid):
            ok, msg = self.tasks.request_cancel(rec.task_id, by_user=uid)
            if not ok:
                continue
            label = f"[{rec.task_type}] {rec.request_text[:20] or rec.task_id}"
            if rec.gradio_job is not None:
                try:
                    await asyncio.to_thread(rec.gradio_job.cancel)
                except Exception as exc:
                    logger.warning(f"取消 Gradio 后端任务失败: {exc}")
            if (
                isinstance(rec.asyncio_task, asyncio.Task)
                and rec.asyncio_task is not asyncio.current_task()
                and not rec.asyncio_task.done()
            ):
                rec.asyncio_task.cancel()
            if rec.status in (ST_RUNNING, ST_CANCELLING):
                self.tasks.terminate_cancelled(rec.task_id)
            stopped.append(f"{label}（{msg}）")
        if stopped:
            lines.append("已发送取消请求：")
            lines.extend("🛑 " + s for s in stopped)
        else:
            lines.append("✅ 你当前没有进行中的翻唱或 TTS 任务。")
        if event.is_admin():
            lines.append("提示：你是管理员，如需取消他人的任务请到 WebUI 任务页操作。")
        return "\n".join(lines)

    @filter.command("cover_status")
    async def cover_status_cmd(self, event: AstrMessageEvent):
        """CUSTOM-8：查询自己的任务状态与全局队列（只能看到自己的任务内容）。"""
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问'查看翻唱任务'即可！")
            return
        uid = self._get_user_id(event)
        running = [t for t in self.tasks.all_tasks() if t.status == "running"]
        queued = self.tasks.pending_count()
        mine = self.tasks.user_tasks(uid, include_finished=True)[:8]
        lines = [f"全局：运行中 {len(running)} · 排队 {queued}（队列上限 {self.tasks.max_pending}）"]
        if not mine:
            lines.append("你当前没有任务记录。")
        for t in mine:
            pos = ""
            if t.status == ST_QUEUED:
                pos = f"（排队第 {self.tasks.queue_position(t.task_id)} 位）"
            line = f"[{t.task_type}] {t.status}{pos} · {t.request_text[:24] or t.task_id}"
            if t.status in ("failed", "timeout") and t.error:
                line += f" · {t.error[:40]}"
            lines.append(line)
        yield event.plain_result("\n".join(lines))

    @filter.command("查看翻唱任务")
    async def view_task_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'查看翻唱任务'即可！")
            return
        result = await self.get_task_status(event)
        await self.selection_ui.send_card(
            event, "⏳ 翻唱任务", result, actions=[("刷新状态", "查看翻唱任务")]
        )

    @filter.command("取消翻唱任务")
    async def cancel_task_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'取消翻唱任务'即可！")
            return
        result = await self.cancel_cover_task(event)
        await self.selection_ui.send_card(event, "🛑 任务取消结果", result)

    # ==================== 辅助命令 ====================
    
    @filter.command("我的翻唱统计")
    async def view_stats_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'查看我的翻唱统计'即可！")
            return
        result = await self.view_my_stats(event)
        await self.selection_ui.send_card(event, "📈 我的翻唱统计", result)

    # ==================== 本地音频翻唱：事件监听 ====================
    
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message_with_audio(self, event: AstrMessageEvent):
        '''监听包含音频的消息，自动触发本地翻唱'''
        if not self.enable_local_audio_cover:
            return

        msg_text = event.message_str.strip()

        # 排除命令消息（让命令处理器自己处理）
        if msg_text.startswith("/") or msg_text.startswith("\\"):
            return

        # 始终尝试提取并缓存音频（无论是否有触发词，方便后续 /本地翻唱 命令使用）
        audio_path = await self._extract_audio_from_event(event)
        if audio_path:
            async with self._audio_cache_lock:
                self._recent_audio_cache[str(event.unified_msg_origin)] = (audio_path, time.time())
            await self._cleanup_expired_audio_cache()
            logger.info(f"[LocalAudio] 已缓存音频: {audio_path}")

        # 检查是否触发自动翻唱
        if not self.local_audio_auto_trigger:
            return

        # 点歌类消息一律不自动翻唱：判定和词表在 intent_gate.py。
        # 缓存音频的那段在前面，所以之后手动发 /本地翻唱 仍然能用最近这条音频。
        if is_music_request(msg_text):
            logger.info("[IntentGate] 点歌类消息，跳过本地音频自动翻唱")
            return

        # 检查消息文字是否包含触发关键词
        has_trigger = any(kw in msg_text for kw in self.local_audio_trigger_keywords)

        # 如果文字没有触发，检查文件名是否包含触发关键词（如上传"处理.mp3"）
        if not has_trigger:
            for comp in getattr(event.message_obj, "message", []):
                if isinstance(comp, (File, Record)):
                    file_name = getattr(comp, "name", "") or ""
                    if any(kw in file_name for kw in self.local_audio_trigger_keywords):
                        has_trigger = True
                        logger.info(f"[LocalAudio] 文件名 '{file_name}' 匹配触发关键词")
                        break

        if not has_trigger:
            return

        if not audio_path:
            logger.info("[LocalAudio] 消息含触发词但无音频，跳过")
            return

        # 解析参数：只认「模型名/序号」和「升降调」，引擎固定 RVC
        words = msg_text.replace("，", " ").replace(",", " ").replace("。", " ").split()
        model_name = self.local_audio_default_model if self.local_audio_default_model else None
        key_shift = self.local_audio_default_shift

        for w in words:
            if w.lstrip('-').isdigit() and -12 <= int(w) <= 12:
                key_shift = int(w)
            elif w not in self.local_audio_trigger_keywords and len(w) >= 2:
                model_name = w

        # 验证模型名，不匹配则使用默认
        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if not matched:
                model_name = None

        # 后台执行，不阻塞事件传播
        self._create_tracked_task(self._execute_local_cover(
            event, audio_path, model_name, key_shift=key_shift
        ))


    # ==================== CUSTOM-7：TTS 语音回复（自然语言触发） ====================

    def _message_mentions_me(self, event: AstrMessageEvent) -> bool:
        """消息是否 @ 了机器人（按消息组件里的 At 判断；拿不到自身 id 时按放行）。"""
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        if not self_id:
            return True
        for comp in getattr(event.message_obj, "message", []) or []:
            if str(getattr(comp, "qq", "") or "") == self_id:
                return True
        return False

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        try:
            if callable(getattr(event, "is_private_chat", None)):
                return bool(event.is_private_chat())
        except Exception:
            pass
        try:
            return not str(event.get_group_id() or "")
        except Exception:
            return False

    @staticmethod
    def _llm_response_text(resp) -> str:
        """从 LLMResponse 取要朗读的文字：completion_text 优先，退回 result_chain 的纯文本。"""
        text = str(getattr(resp, "completion_text", "") or "")
        if text.strip():
            return text
        chain = getattr(resp, "result_chain", None)
        if chain is None:
            return ""
        try:
            parts = []
            for comp in getattr(chain, "chain", None) or []:
                if type(comp).__name__ == "Plain":
                    parts.append(str(getattr(comp, "text", "") or ""))
            return "".join(parts)
        except Exception:
            return ""

    @filter.on_llm_request()
    async def on_llm_request_tts(self, event: AstrMessageEvent, req):
        """TTS 场景提前给主 LLM 打预防针：朗读是用户明确要求的表演。

        没有这条注入时，人格经常在反应文字里直接拒绝（「这句我说不出来」），
        而系统随后又会把那句话用它的音色读出来——反应与语音自相矛盾。
        注入后人格的反应会变成「不情愿但同意」的正确形态。
        """
        try:
            if not bool(self.config.get("tts_enable", False)):
                return
            msg_text = str(event.message_str or "").strip()
            if not msg_text or msg_text.startswith("/") or msg_text.startswith("\\"):
                return
            if is_music_request(msg_text):
                return
            is_tts_perf = is_tts_request(msg_text)
            if not is_tts_perf and not is_cover_request(msg_text) and not has_cover_ticket(msg_text):
                return
            note = (
                "\n[系统提示] 用户要求你朗读一句话（系统会把那句话转成你的音色语音发出）。"
                "请在回复里用你的人设对这句话表达真实反应——不情愿、害羞、吐槽都可以，"
                "但要表现出同意说出来（这是用户明确要求的表演）；"
                "除非内容涉及违法犯罪或严重人身攻击，不要整体拒绝。"
                "你不需要把要说的句子完整写进回复，系统会自动朗读它。"
            )
            existing = getattr(req, "system_prompt", "") or ""
            if not is_tts_perf:
                cover_note = (
                    "\n[系统提示] 用户要求翻唱歌曲。请**立即调用 smart_cover 工具**执行这次翻唱"
                    "（歌名从用户消息提取，升降调/指定音色也从用户消息提取），"
                    "不要只口头答应或说「等我一会儿」——实际的取歌、变声、发送全部由工具完成，"
                    "你不调用工具用户就什么都收不到。对话里之前翻唱过的歌不代表这次不用翻。"
                )
                req.system_prompt = existing + cover_note
                logger.info("[RVC控制台] 已注入翻唱工具调用提示到主 LLM 请求")
            else:
                req.system_prompt = existing + note
                logger.info("[TTS] 已注入朗读表演提示到主 LLM 请求")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[TTS] on_llm_request 注入失败: {exc}")

    @filter.on_llm_response()
    async def on_llm_response_tts(self, event: AstrMessageEvent, resp):
        """用户明确想听机器人说话时，把 LLM 文字回复转成音色语音一起发。

        重要：这里**只 set_result 替换消息链，绝不 stop_event()**——
        AstrBot 4.29 的 OnLLMResponse 钩子在处理阶段执行，stop_event 会连
        Respond 阶段一起拦掉，导致文字和语音都发不出去（消息丢失）。
        """
        try:
            await self._maybe_tts_reply(event, resp)
        except Exception as exc:  # 钩子层必须吞掉一切异常，不影响聊天主流程
            logger.error(f"[TTS] 处理异常（保持原文字回复）: {exc}")

    @staticmethod
    def _speak_from_user_message(user_msg: str) -> str:
        """兜底：从用户消息里直接剥出要说的内容（去 @、去要求语前缀）。"""
        text = str(user_msg or "").strip()
        text = re.sub(r"^@\S+\s*", "", text)
        prev = None
        while prev != text and text:
            prev = text
            stripped = _TTS_REQUIRE_PREFIX_RE.sub("", text, count=1).strip()
            if stripped == text:
                break
            text = stripped
        text = text.strip("，。！!？? 、：:「」『』\"' 　")
        return text[:300]

    async def _extract_speak_text(self, event: AstrMessageEvent, main_reply: str,
                                  user_msg: str = "") -> str:
        """二次 LLM 调用：结合对话上下文，决定这次实际要朗读的内容。

        用户可能是「让我说某句话」「接着说」「复读」——主回复只是人格反应，
        读什么由这次调用结合上文决定。按人设拒绝开口时返回空串（只发反应文字）。
        """
        try:
            umo = event.unified_msg_origin
            provider = self.context.get_using_provider(umo)
            if not provider:
                return ""
            system_prompt = ""
            contexts = []
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                conversation = (
                    await self.context.conversation_manager.get_conversation(umo, cid)
                    if cid else None
                )
                persona_id = getattr(conversation, "persona_id", None)
                try:
                    platform_name = event.get_platform_name() or ""
                except Exception:  # noqa: BLE001
                    platform_name = ""
                chosen = self._find_persona(getattr(self, "notify_persona_id", ""))
                if chosen is None:
                    try:
                        _pid, persona, _force, _web = await self.context.persona_manager.resolve_selected_persona(
                            umo=umo,
                            conversation_persona_id=persona_id,
                            platform_name=platform_name,
                        )
                        chosen = persona
                    except Exception:  # noqa: BLE001
                        chosen = None
                    if self._is_generic_persona(chosen):
                        alt = next(
                            (p for p in (getattr(self.context.persona_manager, "personas_v3", None) or [])
                             if not self._is_generic_persona(p)),
                            None)
                        if alt is not None:
                            chosen = alt
                if chosen:
                    system_prompt = str(chosen.get("system_prompt", "") or "")
                history = getattr(conversation, "message", None) if conversation else None
                if isinstance(history, list) and history:
                    contexts = history[-8:]
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[TTS] 取人格/上下文失败: {exc}")
            # 关键：台词提取**不带人格提示词**。人格强的人设（高冷/拒绝系）会压过
            # 指令导致反复拒绝；人格味道由反应文字负责，这里只做中立的台词提取。
            system_prompt = "你是语音台词提取器，严格按指令输出，不要角色扮演。"

            allow_refuse = bool(self.config.get("tts_allow_refuse", False))
            if allow_refuse:
                refuse_rule = (
                    "4. 如果按你的人设你拒绝开口（反应已经表明拒绝），只输出：空。\n"
                )
            else:
                refuse_rule = ""
            prompt = (
                f"用户发来的原话是：「{str(user_msg or '')[:80]}」\n"
                f"你（机器人）对这个请求的反应已经发出：{str(main_reply or '')[:80]}。\n"
                "现在请给出用户要求朗读的内容。\n"
                "规则：\n"
                "1. 要朗读的内容**必须从用户原话里提取**：去掉「我想听你说」「说一句」"
                "这类要求语，剩下的部分一字不改地输出；\n"
                "2. 绝对禁止自己编造、替换、举例（比如天气、问候、客套话）——"
                "反应里说「换个别的」也不代表你可以自己编内容；\n"
                "3. 只有用户原话是「接着说/再说一遍/继续」这类要求时，才从对话上文里"
                "找该重复或接续的内容；\n"
                "4. 只输出要朗读的内容本身：不要引号、不要表情、不要反应、不要解释；\n"
                + refuse_rule
            )
            llm_resp = None
            try:
                # LLM 服务商可能挂起（限流重试等），不给整个 TTS 链路陪葬
                llm_resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        contexts=contexts or None,
                        system_prompt=system_prompt or None,
                    ),
                    timeout=20,
                )
            except asyncio.TimeoutError:
                logger.warning("[TTS] 朗读内容提取超时（20s），本次只发反应文字")
                return ""
            speak = str(getattr(llm_resp, "completion_text", "") or "").strip()
            logger.info("[TTS] 朗读内容提取原始输出: %r", speak[:120])
            speak = speak.strip('“”"\'『』「」` 　\n')
            if self.config.get("tts_allow_refuse", False):
                if (not speak) or speak in ("空", "（空）", "(空)", "无", "（无）"):
                    return ""
            else:
                # 「允许拒绝=关」：提取不到/拒绝话术时，退化为从用户消息直取，
                # 保证「让我说X」场景的语音一定能出
                if (not speak) or speak in ("空", "（空）", "(空)", "无", "（无）"):
                    fallback = self._speak_from_user_message(user_msg)
                    logger.info("[TTS] 提取为空，用户消息直取兜底: %r", fallback[:40])
                    return fallback
                if _TTS_REFUSE_RE.search(speak):
                    fallback = self._speak_from_user_message(user_msg)
                    logger.info("[TTS] 提取为拒绝话术，用户消息直取兜底: %r", fallback[:40])
                    return fallback
            return speak[:300]
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[TTS] 提取朗读内容失败: {exc}")
            return ""

    async def _maybe_tts_reply(self, event: AstrMessageEvent, resp) -> None:
        # 触发相关配置实时读取：WebUI 改完即生效，无需重载插件
        enable = bool(self.config.get("tts_enable", False))
        text = self._llm_response_text(resp)
        msg_text = str(event.message_str or "").strip()
        logger.info(
            "[TTS] 钩子触发 | enable=%s | reply_len=%s | msg=%r",
            enable, len(text), msg_text[:50],
        )
        if not enable:
            return
        if not msg_text or msg_text.startswith("/") or msg_text.startswith("\\"):
            return
        # 防重复：同一条事件只处理一次
        if getattr(event, "_chorus_tts_done", False):
            return
        # 意图判定（intent_gate，顺序：点歌 > 翻唱 > TTS）
        if is_music_request(msg_text):
            logger.info("[TTS] 判定为点歌需求，跳过")
            return
        if has_cover_ticket(msg_text):
            logger.info("[TTS] 消息含翻唱词，交给翻唱链路")
            return  # 「唱」「翻唱」「变声」优先走翻唱链路，绝不误判成 TTS
        if not is_tts_request(msg_text):
            logger.info("[TTS] 未命中 TTS 触发表达，跳过")
            return
        # 平台与触发范围
        is_private = self._is_private_chat(event)
        if is_private:
            if not bool(self.config.get("tts_private_trigger", True)):
                return
        else:
            if not bool(self.config.get("tts_group_trigger", True)):
                return
            if bool(self.config.get("tts_group_require_at", True)) and not self._message_mentions_me(event):
                logger.info("[TTS] 群聊消息未 @Bot，跳过")
                return
        # 翻唱进行中也不阻塞 TTS 判定；并发由 TTS 队列与网关串行槽共同控制
        user_id, user_name = self._sender_pair(event)
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        # 复读间隔：上一条语音实际发出后 N 秒内不再触发（独立于用户/群冷却）
        try:
            repeat_delay = max(0, int(self.config.get("tts_repeat_delay", 0) or 0))
        except (TypeError, ValueError):
            repeat_delay = 0
        if repeat_delay > 0:
            elapsed_since_voice = time.time() - float(getattr(self, "_tts_last_voice_at", 0.0) or 0.0)
            if 0 < elapsed_since_voice < repeat_delay:
                logger.info(
                    "[TTS] 复读间隔中未触发（语音发出后 %.0fs / 间隔 %ds）",
                    elapsed_since_voice, repeat_delay)
                return
        ok, reason = self.tts_cooldown.check(user_id, group_id)
        if not ok:
            logger.info(f"[TTS] 冷却中未触发: {reason}")
            return
        # 冷却时长也实时读取（改配置即生效；时间戳记录保留）
        try:
            self.tts_cooldown.user_seconds = max(0, int(self.config.get("tts_user_cooldown", 60) or 0))
            self.tts_cooldown.group_seconds = max(0, int(self.config.get("tts_group_cooldown", 30) or 0))
        except (TypeError, ValueError):
            pass
        event._chorus_tts_done = True
        self.tts_cooldown.mark(user_id, group_id)
        # CUSTOM-8：防重复提交 + 统一任务注册（任务追踪关闭时不登记）
        rec = None
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if self.enable_task_tracking:
            dup = self.tasks.find_duplicate(user_id, umo, msg_text)
            if dup is not None:
                logger.info(f"[TTS] 重复提交被拦截（existing={dup.task_id}）")
                return
            rec = self.tasks.register(
                task_type="tts", conversation_id=umo, user_id=user_id,
                user_name=user_name, group_id=group_id, message_id=message_id,
                request_text=msg_text)
            if rec is None:
                logger.info("[TTS] 全局任务队列已满，跳过")
                return
        # ① 人格反应文字立刻发出（主 LLM 回复本身就是带上下文的反应）
        reaction_sent = False
        try:
            await event.send(event.chain_result([Plain(text)]))
            reaction_sent = True
        except Exception as send_exc:
            logger.error(f"[TTS] 反应文字发送失败: {send_exc}")
        if rec:
            self.tasks.set_status(rec.task_id, ST_QUEUED)
        ahead = len(self._tts_queue) + (1 if self._tts_slot.locked() else 0)
        # ② 入队（与翻唱队列同构：同一时间一个转换，其余排队，满载静默跳过）
        if not await self._acquire_tts_slot(
                str(event.unified_msg_origin), (msg_text or text)[:24],
                meta={"user": user_id, "user_name": user_name,
                      "registry_id": rec.task_id if rec else "",
                      "text": (msg_text or text)[:60]}):
            logger.info("[TTS] TTS 排队已满（上限 %d），本次跳过",
                        int(getattr(self, "tts_queue_limit", 3)))
            if reaction_sent:
                event.stop_event()
            return
        try:
            self._set_tts_stage("提取台词")
            if rec:
                self.tasks.begin_by_id(rec.task_id)
                if rec.cancel_requested:
                    self.tasks.terminate_cancelled(rec.task_id)
                    logger.info("[TTS] 任务在排队期间被取消")
                    return
            if ahead > 0 and bool(self.config.get("queue_position_notice", True)):
                try:
                    await event.send(event.plain_result(f"已加入队列，前面还有 {ahead} 个任务。"))
                except Exception:  # noqa: BLE001
                    pass
            # ③ 结合上下文提取实际要朗读的内容（支持「说某句/接着说/复读」；拒绝时只发反应）
            speak_text = await self._extract_speak_text(event, text, user_msg=msg_text)
            if not speak_text:
                logger.info("[TTS] 人格决定不朗读，仅发送反应文字")
                return
            if rec and rec.cancel_requested:
                self.tasks.terminate_cancelled(rec.task_id)
                logger.info("[TTS] 任务在提取期间被取消")
                return
            try:
                max_chars = int(self.config.get("tts_max_text_chars", 200) or 200)
            except (TypeError, ValueError):
                max_chars = 200
            if len(speak_text) > max_chars:
                speak_text = speak_text[:max_chars]
            # CUSTOM-9.1:任务名只显示“说的内容”,不带触发语(排队中与运行中同步更新)
            for q in self._tts_queue:
                if rec and q.get("registry_id") == rec.task_id:
                    q["label"] = speak_text[:40]
                    q["text"] = speak_text[:60]
            if self._tts_active is not None:
                self._tts_active["label"] = speak_text[:40]
                self._tts_active["text"] = speak_text[:60]
            # ④ 朗读内容 → TTS → RVC 变声
            self._set_tts_stage("TTS 生成与变声")
            _, out_path, info = await self.tts.convert(speak_text, event)
            self._set_tts_stage("发送语音")
            if rec and rec.cancel_requested:
                self.tasks.terminate_cancelled(rec.task_id)
                logger.info("[TTS] 任务在转换完成后被取消（成品已入 out，可经 /补发 找回）")
                return
        except Exception as exc:
            logger.error(f"[TTS] 语音生成失败（反应文字已发）: {exc}")
            if rec:
                try:
                    self.tasks.finish(rec, error=str(exc))
                except Exception:  # noqa: BLE001
                    pass
            return
        finally:
            self._release_tts_slot()
            if reaction_sent:
                event.stop_event()  # 反应文字已发出（无论成败），阻止 Respond 阶段重复发送
        # ⑤ 发语音；朗读内容注回对话记忆，保证后续「接着说/复读」有据可依
        try:
            await event.send(event.chain_result([Record(file=out_path)]))
            self._tts_last_voice_at = time.time()
            logger.info("[TTS] 已发送反应文字与语音（朗读: %r）", speak_text[:40])
        except Exception as send_exc:
            logger.error(f"[TTS] 语音发送失败: {send_exc}")
        try:
            if rec:
                rec.output_path = out_path
                rec.gateway_task_id = str((info or {}).get("task_id") or "")
                self.tasks.finish(rec)
            group_id, group_name = await self._group_pair(event)
            self._record_tts_history(
                task_id=str((info or {}).get("task_id") or uuid.uuid4().hex[:12]),
                user=user_id,
                user_name=user_name,
                group_id=group_id,
                group_name=group_name,
                request_text=speak_text,
                speak_text=speak_text,
                model=str((info or {}).get("model") or self.config.get("tts_rvc_model", "") or ""),
                output_path=out_path,
                duration=(info or {}).get("output_duration"))
            if getattr(self, "enable_inject_context", True):
                await self._inject_message_to_context(
                    event, f"（你刚才朗读出了：{speak_text}）", role="assistant")
        except Exception:  # noqa: BLE001
            pass
        event.stop_event()  # 反应文字与语音均已自行发送，阻止 Respond 阶段重复发送

    async def terminate(self):
        """插件卸载时清理资源"""
        logger.info("[RVC控制台] 正在清理资源...")
        pending = [t for t in getattr(self, "_pending_tasks", set()) if not t.done()]
        running = [k for k in getattr(self, "_active_cover_tasks", {})]
        if pending or running:
            logger.warning(
                "[RVC控制台] 重载/卸载会中断 %d 个进行中的翻唱任务（会话: %s）；"
                "已生成的成品仍保留在 %s，重载后可用 /补发翻唱 <task_id> 取回，无需重跑推理",
                len(pending), ", ".join(running) or "-", self._gateway_out_dir() or "(未配置)")
        await self.selection_ui.close()
        
        # 取消所有待处理的任务
        if hasattr(self, '_pending_tasks') and self._pending_tasks:
            for task in list(self._pending_tasks):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=2)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
            self._pending_tasks.clear()
        
        # 保存偏好数据
        try:
            if hasattr(self, '_pref_lock') and self.enable_preference_learning:
                await self._save_preferences()
        except Exception as e:
            logger.error(f"[RVC控制台] 卸载时保存偏好失败: {e}")
        
        # 清理API session
        try:
            if hasattr(self, 'song_router') and self.song_router:
                await self.song_router.close()
        except Exception as e:
            logger.debug(f"[RVC控制台] 关闭音源路由 session: {e}")

        try:
            if hasattr(self, 'netease_fetcher') and self.netease_fetcher:
                await self.netease_fetcher.close()
        except Exception as e:
            logger.debug(f"[RVC控制台] 关闭取歌 session: {e}")

        logger.info("[RVC控制台] 资源清理完成")
