"""统一对话出口（chorus_cover 自维护分支）。

本模块把原本硬编码、直接 ``event.send`` 给用户的三类文本收敛成一个出口：

- 进度提示（progress）
- 风控重试提示（retry）
- 报错详情（failed）
- 配置失效提醒（alert，发给管理员）

设计约定：

1. ``progress`` / ``retry`` 默认不发送，由配置项 ``notify_progress`` / ``notify_retry``
   控制，缺省 ``False``——被关掉时一条都不发，直接 ``return``。
2. ``start`` / ``failed`` / ``done`` 才真正说话：插件只把事实（歌名、模型、来源、
   失败原因等）拼成清单，台词交给当前人格模型现编，避免固定模板的"机器味"。
3. 出口层自身的任何异常（拿不到 provider、模型调用失败、发送失败……）都必须吞掉，
   退回一句极短兜底文本，绝不能让翻唱主流程因为"说话"而崩掉；失败只记 warning。
4. 所有发送都走 ``await event.send(event.plain_result(text))``。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from astrbot.api import logger

#: 支持的出口类型
VALID_KINDS = ("start", "progress", "retry", "failed", "done", "alert")

#: 需要开关控制的类型 -> (配置项, 缺省值)
_SWITCHES: Dict[str, tuple] = {
    "progress": ("notify_progress", False),
    "retry": ("notify_retry", False),
}

#: 事实清单字段的中文标签
_FACT_LABELS: Dict[str, str] = {
    "song": "歌名",
    "model": "用的音色",
    "source": "歌曲来源",
    "reason": "没成的原因",
    "step": "现在做到哪一步",
    "detail": "补充情况",
    "count": "第几首",
    "total": "一共几首",
    "seed": "随机种子",
    "artist": "歌手",
}

#: 每种类型交给模型的任务说明
_KIND_HINT: Dict[str, str] = {
    "start": "刚接下这件事，跟用户打个招呼，说这就去做",
    "progress": "事情还在做，跟用户说一声现在到哪一步了",
    "retry": "第一次没成功，正在自动再试一次，跟用户说一句别急",
    "failed": "这件事没做成，得告诉用户没成，并给一句人话的理由",
    "done": "事情做完了，跟用户说一声",
    "alert": "这是在提醒对方（管理员）：某个配置好像失效了，请他有空去更新一下",
}

#: 兜底文本：模型不可用 / 出口出错时用，必须极短
FALLBACK_TEXT: Dict[str, str] = {
    "start": "好，这就去弄",
    "progress": "还在弄，稍等我一下",
    "retry": "再等我一下",
    "failed": "这首没唱成，换个歌名我再试一次",
    "done": "弄好了，听听看",
    "alert": "网易云的 cookie 好像失效了，你有空更新一下",
}

#: 人格提示词
SYSTEM_PROMPT = (
    "你在跟聊天里的这个人说话，请完全按你当前的人格和说话习惯，只说一句话。"
    "要口语，像人随口聊天，不要复述参数，不要罗列技术细节，不要换行，"
    "不要用表情符号，也不要用红叉、沙漏、对勾这类标记。"
    "不要提“插件”“后端”“API”“风控”这些词，用日常说法代替。"
    "如果要说的是没做成，就直接说这首没唱成，再给一句人话的理由，"
    "别解释是哪个环节坏了，也别道歉三次。"
)

#: 需要从事实里剔除的符号（emoji / 装饰字符）
_SYMBOL_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2300-\u23FF\u25A0-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)


def _clean_fact(value: Any, limit: int = 120) -> str:
    """把事实值洗成一行纯文字：去符号、压空白、截断。"""
    if value is None:
        return ""
    text = str(value)
    text = _SYMBOL_RE.sub(" ", text)
    text = text.replace("❌", " ").replace("⏳", " ").replace("✅", " ")
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-—：:")
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


class Outbound:
    """插件唯一的对话出口。

    Args:
        plugin: 插件实例，需要能拿到 ``self.context`` 与 ``self.config``。
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin

    # ------------------------------------------------------------------ 开关
    @property
    def _config(self) -> Any:
        return getattr(self.plugin, "config", None)

    @property
    def _context(self) -> Any:
        return getattr(self.plugin, "context", None)

    def _switch(self, key: str, default: bool = False) -> bool:
        """读取布尔开关；配置拿不到时按缺省值处理。"""
        try:
            config = self._config
            if config is None:
                return default
            value = config.get(key, default)
            return bool(value)
        except Exception:
            logger.warning("统一出口读取配置项 %s 失败，按缺省值处理", key, exc_info=True)
            return default

    # ------------------------------------------------------------------ 出口
    async def say(self, event: Any, kind: str, **facts: Any) -> bool:
        """说一句话。

        Args:
            event: AstrMessageEvent，或实现了 ``plain_result`` / ``send`` /
                ``unified_msg_origin`` 的同形对象（用于主动发给指定会话）。
            kind: 出口类型，取值 start / progress / retry / failed / done / alert。
            **facts: 交给模型的事实，如 song、model、source、reason、step 等。

        Returns:
            是否真的发出去了。``progress`` / ``retry`` 被开关静音时返回 False，
            发送异常也返回 False，方便调用方降级到别的会话重发。
        """
        normalized = str(kind or "").strip().lower()
        if normalized not in VALID_KINDS:
            logger.warning("统一出口收到未知类型 %r，按静音处理", kind)
            return False

        # progress / retry 默认静音：关掉时一条都不发
        switch = _SWITCHES.get(normalized)
        if switch is not None:
            config_key, default = switch
            if not self._switch(config_key, default):
                return False

        text = await self._compose(event, normalized, facts)
        if not text:
            text = FALLBACK_TEXT.get(normalized, "嗯，我知道了")

        try:
            sent = await event.send(event.plain_result(text))
        except Exception:
            # 发送失败也不能影响翻唱流程
            logger.warning("统一出口发送失败（已忽略），kind=%s", normalized, exc_info=True)
            return False
        return sent is not False

    # ------------------------------------------------------------------ 内部
    async def _compose(self, event: Any, kind: str, facts: Dict[str, Any]) -> str:
        """让当前人格模型把事实说成一句话；任何异常都返回空串（交由兜底）。"""
        try:
            provider = self._get_provider(event)
            if provider is None:
                logger.warning("统一出口拿不到 provider，改用兜底文本（kind=%s）", kind)
                return ""
            prompt = self._build_prompt(kind, facts)
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
                contexts=[],
            )
            return self._extract_text(response)
        except Exception:
            logger.warning("统一出口调用模型失败，改用兜底文本（kind=%s）", kind, exc_info=True)
            return ""

    def _get_provider(self, event: Any = None) -> Any:
        """取当前会话的人格 provider；取不到返回 None。"""
        context = self._context
        getter = getattr(context, "get_using_provider", None)
        if getter is None:
            return None
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            try:
                provider = getter(umo)
                if provider is not None:
                    return provider
            except TypeError:
                # 旧版本不接受 umo 参数，下面走无参调用
                pass
            except Exception:
                logger.warning("按会话取 provider 失败，回退到默认 provider", exc_info=True)
        return getter()

    @staticmethod
    def _build_prompt(kind: str, facts: Dict[str, Any]) -> str:
        """把事实拼成一段清单交给模型，不给现成模板。"""
        lines = [f"这一刻的情况：{_KIND_HINT.get(kind, '跟用户说一声')}", "已知事实："]
        for key, value in facts.items():
            cleaned = _clean_fact(value)
            if not cleaned:
                continue
            label = _FACT_LABELS.get(key, str(key))
            lines.append(f"- {label}：{cleaned}")
        lines.append("现在请用一句话对用户说，不要换行，不要列举技术细节。")
        return "\n".join(lines)

    @staticmethod
    def _extract_text(response: Any) -> str:
        """从 LLM 响应里取出正文，并压成单行短句。"""
        if response is None:
            return ""
        text: Optional[str] = None
        if isinstance(response, str):
            text = response
        else:
            text = getattr(response, "completion_text", None)
        if not text:
            return ""
        text = str(text).strip()
        # 只保留第一行，去掉常见符号和引号
        text = text.splitlines()[0] if text else ""
        text = _clean_fact(text, limit=120)
        text = text.strip("“”\"'`")
        return text
