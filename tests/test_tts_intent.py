# -*- coding: utf-8 -*-
"""CUSTOM-7：TTS / 翻唱 / 点歌意图判定回归测试。

只依赖标准库（intent_gate 不 import astrbot），本地与 NAS 都能跑：
    python tests/test_tts_intent.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intent_gate import (  # noqa: E402
    has_cover_ticket,
    has_music_ticket,
    is_tts_request,
)


def expect_tts(text: str) -> None:
    assert is_tts_request(text), f"应为 TTS: {text!r}"


def expect_not_tts(text: str, why: str = "") -> None:
    assert not is_tts_request(text), f"不应为 TTS({why}): {text!r}"


def main() -> int:
    # ---- 方案 8.2 的 TTS 触发语句，全部必须命中 ----
    for text in (
        "我想听你说",
        "我想听你说句话",
        "你能对我说句话吗",
        "你能对我说说吗",
        "用声音回复我",
        "我想听你的声音",
        "说说给我听听",
        "说给我听听",
        "说一句给我听",
        "说句话给我听",
        "能不能语音回复",
        "发个语音听听",
        "来点语音",
        "语音说一句",
        "说句来听听",
    ):
        expect_tts(text)

    # ---- 歌曲翻唱语句：优先翻唱，绝不误判成 TTS ----
    for text in (
        "我想听你唱",
        "你唱一下这首歌",
        "唱一下晴天的高潮",
        "唱一句给我听",
        "帮我翻唱这首歌",
        "用三月七的音色唱",
        "变声吧",
        "翻唱《雨爱》",
    ):
        expect_not_tts(text, "翻唱优先")
        assert has_cover_ticket(text), f"应识别为翻唱词: {text!r}"

    # ---- 点歌语句：先被点歌闸门拦截，与 TTS 无关 ----
    for text in (
        "点歌 晴天",
        "来一首告白气球",
        "放首歌",
        "点首歌",
    ):
        expect_not_tts(text, "点歌优先")
        assert has_music_ticket(text), f"应识别为点歌词: {text!r}"

    # ---- 普通聊天 / 模糊表达：一律不触发 ----
    for text in (
        "今天天气怎么样",
        "你觉得人生有什么意义",
        "你说呢",
        "听说了吗",
        "他的声音很好听",
        "说句实话",
        "我在听歌",
        "这个歌手唱得不错",
        "把音量调小一点",
        "给我讲个道理",
        "打开语音",
        "",
        "12345",
    ):
        expect_not_tts(text, "普通聊天/模糊")

    # ---- 边界：点歌 + 说话组合（点歌优先）----
    expect_not_tts("点歌然后说句话给我听", "点歌优先")

    print("test_tts_intent: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
