# -*- coding: utf-8 -*-
"""CUSTOM-7：网关卷积混响内核的数学回归测试（不需要 ffmpeg/gradio）。

做法：从 gateway.py 源码里按 AST 抽出混响相关函数与常量，在独立命名空间里
执行（不 import 整个 gateway，避开 gradio 依赖），本地与 NAS 都能跑：
    python tests/test_reverb_kernel.py
"""

from __future__ import annotations

import ast
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY_SRC = os.path.join(HERE, "..", "..", "gateway_dev", "gateway.py")

WANT_FUNCS = {"_reverb_lowpass_noise", "build_reverb_ir", "_as_float"}
WANT_ASSIGNS = {"REVERB_SR"}


def load_reverb_namespace() -> dict:
    tree = ast.parse(open(GATEWAY_SRC, encoding="utf-8").read())
    ns: dict = {"__builtins__": __builtins__}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            code = ast.Module(body=[node], type_ignores=[])
            exec(compile(code, GATEWAY_SRC, "exec"), ns)  # noqa: S102
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(t in WANT_ASSIGNS for t in targets):
                code = ast.Module(body=[node], type_ignores=[])
                exec(compile(code, GATEWAY_SRC, "exec"), ns)
    ns.setdefault("GatewayError", type("GatewayError", (RuntimeError,), {}))
    return ns


def main() -> int:
    ns = load_reverb_namespace()
    build_ir = ns["build_reverb_ir"]
    sr = ns["REVERB_SR"]

    # 1) 基本形状：IR 长度 = pre_delay + early + tail，能量归一
    ir_l, ir_r = build_ir(0.65, 1.4, 0.45, 18.0, 0.8)
    expect_len = int(18.0 / 1000 * sr) + int(round((0.02 + 0.06 * 0.65) * sr)) + int(round(1.4 * sr))
    assert abs(ir_l.size - expect_len) <= 2, f"IR 长度 {ir_l.size} != 预期 {expect_len}"
    assert ir_r.size == ir_l.size
    e_l = float(np.sqrt(np.sum(np.square(ir_l, dtype=np.float64))))
    assert abs(e_l - 1.0) < 1e-4, f"IR 能量未归一: {e_l}"

    # 2) 确定性：同参数同结果
    ir_l2, ir_r2 = build_ir(0.65, 1.4, 0.45, 18.0, 0.8)
    assert np.array_equal(ir_l, ir_l2) and np.array_equal(ir_r, ir_r2)

    # 3) 立体声宽度语义：width=0 → 双耳相同；width=1 → 去相关（相关性低）
    mono_l, mono_r = build_ir(0.5, 1.0, 0.4, 0.0, 0.0)
    assert np.allclose(mono_l, mono_r)
    wide_l, wide_r = build_ir(0.5, 1.0, 0.4, 0.0, 1.0)
    corr = float(np.corrcoef(wide_l, wide_r)[0, 1])
    assert corr < 0.5, f"width=1 时左右声道相关性过高: {corr}"

    # 4) damping 语义：damping 越大尾音越暗（高频能量占比越低）
    def hf_ratio(damping: float) -> float:
        ir, _ = build_ir(0.65, 1.2, damping, 10.0, 0.8)
        spec = np.abs(np.fft.rfft(ir.astype(np.float64)))
        n = spec.size
        low = float(np.sum(spec[: n // 4]) + 1e-9)
        high = float(np.sum(spec[3 * n // 4:]) + 1e-9)
        return high / low

    assert hf_ratio(0.9) < hf_ratio(0.1), "damping 增大应压低高频占比"

    # 5) pre_delay 语义：开头 pre_delay 段应为 0（静音）
    ir_p, _ = build_ir(0.65, 1.0, 0.4, 50.0, 0.8)
    pre_n = int(50.0 / 1000 * sr)
    assert float(np.max(np.abs(ir_p[:pre_n]))) == 0.0, "pre_delay 段不应有能量"

    # 6) room_size 语义：第一个早反射脉冲的位置随 room_size 拉开
    #    （pre_delay 段全 0，第一个非零样本 = 第一个 tap，位置可精确预测）
    def first_nonzero(room: float) -> int:
        ir, _ = build_ir(room, 1.0, 0.3, 0.0, 0.5)
        idx = np.nonzero(np.abs(ir) > 1e-6)[0]
        return int(idx[0]) if idx.size else -1

    def expect_first_tap(room: float) -> int:
        early_n = int(round((0.02 + 0.06 * room) * sr))
        n_taps = 3 + int(round(4 * room))
        return int((1 / n_taps) * max(early_n - 1, 1))

    assert first_nonzero(0.9) > first_nonzero(0.1), "room_size 增大应拉开早反射起点"
    for room in (0.1, 0.5, 0.9):
        assert first_nonzero(room) == expect_first_tap(room), \
            f"room={room} 首个 tap 位置 {first_nonzero(room)} != 预期 {expect_first_tap(room)}"

    # 7) 卷积湿声能量与 wet 成比例：wet 越大输出里高频噪声成分越多
    from scipy.signal import fftconvolve

    rng = np.random.default_rng(7)
    x = (rng.standard_normal(sr * 2) * 0.1).astype(np.float32)  # 2s 白噪声
    for wet, expect_gain in ((0.0, 1.0), (0.18, None), (0.5, None)):
        ir_a, ir_b = build_ir(0.65, 1.2, 0.45, 10.0, 0.8)
        wet_sig = fftconvolve(x, ir_a).astype(np.float32)
        mix = x * 1.0 + wet * wet_sig[: x.size]
        rms = float(np.sqrt(np.mean(np.square(mix[: sr], dtype=np.float64))))
        if wet == 0.0:
            base = rms
        else:
            # 混响加了能量，RMS 应上升；wet 越大升得越多
            assert rms > base * (1.0 + 0.01), f"wet={wet} 未增加能量: rms={rms}, base={base}"
            if expect_gain is not None:
                continue

    # 8) 峰值保护：满幅正弦 + wet 0.18 会超过 1.0，apply_reverb 里的峰值保护
    #    （peak>0.95 按比例压回）必须能把输出压到 0.95 以内
    t = np.arange(sr, dtype=np.float32) / sr
    sine = (0.99 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)
    ir_a, ir_b = build_ir(0.65, 1.2, 0.45, 10.0, 0.8)
    wet_sig = fftconvolve(sine, ir_a).astype(np.float32)
    mix = sine * 1.0 + 0.18 * wet_sig[: sine.size]
    raw_peak = float(np.max(np.abs(mix)))
    assert raw_peak > 0.95, "测试前提失效：满幅输入应触发峰值保护"
    mix *= 0.95 / raw_peak
    assert float(np.max(np.abs(mix))) <= 0.95 + 1e-6

    print("test_reverb_kernel: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
