"""点歌 / 翻唱需求判定闸门（本插件唯一词表来源）。

为什么要有这个文件：

    点歌插件（astrbot_plugin_music）的命令匹配要求「点歌」出现在命令位置。
    像「嗯。点歌。失眠飞行。」这种把点歌夹在句子中间的消息不会命中命令，
    会落到大模型手里；模型看到 smart_cover 这类翻唱工具，很容易顺手调用，
    把「点歌」变成「翻唱」。反过来，「点歌 晴天」偶尔也会因为命令没命中而
    被翻唱入口接走。

    这里把判定收成一处：消息里出现点歌类词、且没有翻唱类词，就判定为
    「点歌需求」，所有翻唱入口（LLM 工具 / 本地音频自动触发）一律拒绝执行，
    只把事实回给模型，由模型引导用户用点歌命令（或直接调用点歌插件的
    play_song_by_name 工具）。

以后加词/删词只改下面两个 TICKET_WORDS_* 常量。本模块只依赖标准库，
不 import astrbot，方便用假 event 单独跑判定测试。
"""

from __future__ import annotations

import re
from typing import Any, Optional

# === 翻唱类词 ===
# 出现任意一个，就认为用户确实想「唱」，不再按点歌拦截。
# 「唱」一个字已经覆盖「翻唱」「唱一首」「用三月七唱」等写法；
# 「翻唱」「翻唱成」重复列出只为让词表一眼看懂，不影响结果。
TICKET_WORDS_COVER: tuple[str, ...] = (
    "唱",
    "翻唱",
    "翻唱成",
    "变声",
    "cover",
)

# === 点歌类词 ===
# 出现任意一个、且没有翻唱类词时，判定为点歌需求。
TICKET_WORDS_MUSIC: tuple[str, ...] = (
    "点歌",
    "放歌",
    "听歌",
    "来一首",
    "放一首",
    "播一首",
    # 下面是把「一」省掉或换成「首」的常见说法（如「给我放首歌」）
    "点一首",
    "点首歌",
    "来首歌",
    "放首歌",
    "播首歌",
)

# 词表之外再补两条正则，兜住「放首音乐」这类不常见的省略写法。
TICKET_PATTERNS_MUSIC: tuple[str, ...] = (
    r"(点|放|播|来)(一)?首?歌",
    r"(点|放|播|来)(一)?首(曲|音乐)",
)

# 「用…唱」本来就含「唱」，单列一条是为了把这条口语写法固定下来。
TICKET_PATTERNS_COVER: tuple[str, ...] = (
    r"用.{1,16}唱",
)

# === TTS 语音回复类（CUSTOM-7）===
# 用户希望「听机器人说话」的表达。判定从宽收词、从严匹配：
# 单独的「说」「听」「声音」绝不触发，必须是下面这些整句式组合。
TICKET_PATTERNS_TTS: tuple[str, ...] = (
    r"听.{0,4}你说",              # 我想听你说 / 想听你说句话
    r"听.{0,3}(你的)?声音",       # 我想听你的声音 / 听听你的声音
    r"对.{0,3}我(说|讲)",         # 你能对我说句话吗 / 对我说说
    r"说.{0,4}给.{0,2}我.{0,4}听",  # 说一句给我听 / 说给我听听 / 说句话给我听
    r"(说|讲)句.{0,6}听",         # 说句来听听
    r"用.{0,4}声音.{0,6}(回复|回答|说|讲话|讲)",
    r"(声音|语音)回复",           # 声音回复我 / 能不能语音回复
    r"(发|来|用|要|听|来点)个?.{0,2}语音",  # 发个语音 / 来点语音
    r"语音.{0,2}(说|讲|聊|回)",   # 语音说一句
    r"(讲|说)话.{0,3}我听",
    r"(要|想|让)你(说|讲|念|读)",  # 我要你说 / 想你念一句(CUSTOM-9 词表补充)
    r"让.{0,4}睦(说|讲|念|读)",    # 让睦说一句(按人名称呼)       # 说说话给我听
    # CUSTOM-7.3：直接命令式（说一下X / 你念一遍X / 说：X）与复读（再来一遍/复读）
    r"^(说|讲|念|读)(一下|一遍|一句|一声)",
    r"(帮我|给我|你来|你)(说|讲|念|读)(一下|一遍|一句|一声)",
    r"(说|讲|念|读)(一下|一遍|一句|一声)?[:：「『\"']",
    r"(再|重新)(说|读|念|来)一?(遍|次|句)",
    r"复读",
)

# 判定为 TTS 需求时回给模型/日志的说明。
TTS_REQUEST_HINT = "这是想听机器人说话（TTS 语音回复），不是翻唱，不要调用任何翻唱工具。"

# 判定为点歌需求时回给模型的话。这是工具返回值（事实+指令），
# 不是直接发给用户的聊天回复；最终怎么说由人格模型组织。
MUSIC_REQUEST_HINT = (
    "这是点歌需求，不是翻唱：用户想听原曲，不要执行翻唱，也不要再调用其它翻唱工具。"
    "请让用户用「点歌 歌名」命令点原曲；如果 play_song_by_name 工具可用，也可以直接用它播放原曲。"
)


def _normalize(text: Any) -> str:
    """统一成小写字符串；None、非字符串一律当空串。"""
    return str(text or "").casefold()


def has_music_ticket(text: str) -> bool:
    """文本里是否出现点歌类词。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    if any(word in normalized for word in TICKET_WORDS_MUSIC):
        return True
    return any(re.search(pattern, normalized) for pattern in TICKET_PATTERNS_MUSIC)


def has_cover_ticket(text: str) -> bool:
    """文本里是否出现翻唱类词。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    if any(word in normalized for word in TICKET_WORDS_COVER):
        return True
    return any(re.search(pattern, normalized) for pattern in TICKET_PATTERNS_COVER)


def is_music_request(text: str) -> bool:
    """消息是不是点歌需求：有点歌词、且没有翻唱词。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    return has_music_ticket(normalized) and not has_cover_ticket(normalized)


def has_tts_ticket(text: str) -> bool:
    """文本里是否出现 TTS 语音回复类表达。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in TICKET_PATTERNS_TTS)


def is_tts_request(text: str) -> bool:
    """消息是不是「想听机器人说话」。

    判定顺序（CUSTOM-7，与升级方案 8.2 一致）：
    点歌拒绝 > 歌曲翻唱 > TTS。含点歌或翻唱词时一律不算 TTS，
    「唱一句给我听」这类表达交给翻唱链路按现有规则处理。
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    if has_music_ticket(normalized) or has_cover_ticket(normalized):
        return False
    return has_tts_ticket(normalized)


def is_cover_request(text: str) -> bool:
    """消息是不是翻唱需求：有翻唱词（点歌词已被 is_music_request 先行拦截）。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    return has_cover_ticket(normalized) and not has_music_ticket(normalized)


def message_text(event: Any) -> str:
    """从事件里取当前消息文本；取不到或不是字符串时返回空串（按放行处理）。"""
    try:
        return str(getattr(event, "message_str", "") or "")
    except Exception:
        return ""


def cover_refusal(event: Any) -> Optional[str]:
    """翻唱入口统一闸门：点歌需求返回提示句，其它情况返回 None 表示放行。"""
    return MUSIC_REQUEST_HINT if is_music_request(message_text(event)) else None
