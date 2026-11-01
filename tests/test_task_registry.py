# -*- coding: utf-8 -*-
"""CUSTOM-8：统一任务注册表的并发语义回归测试（纯标准库，本地与 NAS 均可跑）。

    python tests/test_task_registry.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from task_registry import (  # noqa: E402
    TaskRegistry,
    ST_RECEIVED, ST_QUEUED, ST_RUNNING, ST_CANCELLING,
    ST_COMPLETED, ST_FAILED, ST_CANCELLED, ST_TIMEOUT, ST_REJECTED,
)


def make_registry(**kw) -> TaskRegistry:
    return TaskRegistry(**kw)


def main() -> int:
    # 1) FIFO：先提交先排队
    reg = make_registry()
    recs = [reg.register("cover", f"grp:{i}", user_id=f"u{i}", request_text=f"歌{i}")
            for i in range(4)]
    assert all(r is not None for r in recs)
    for r in recs:
        reg.set_status(r.task_id, ST_QUEUED)
    assert reg.queue_position(recs[0].task_id) == 1
    assert reg.queue_position(recs[3].task_id) == 4
    # 跨会话不丢顺序：不同 conversation 混排后仍按 created_at
    assert [r.task_id for r in sorted(
        (t for t in reg.all_tasks() if t.status == ST_QUEUED),
        key=lambda t: t.created_at)] == [r.task_id for r in recs]

    # 2) 全局上限：等待数达 max_pending 后拒绝（跨类型共享）
    reg = make_registry(max_pending=3)
    a = reg.register("cover", "g1", user_id="u1", request_text="A")
    b = reg.register("tts", "g2", user_id="u2", request_text="B")
    c = reg.register("cover", "g1", user_id="u3", request_text="C")
    assert all(x is not None for x in (a, b, c))
    d = reg.register("tts", "g3", user_id="u4", request_text="D")
    assert d is None, "超过全局等待上限应返回 None"
    reg.set_status(a.task_id, ST_QUEUED)
    reg.set_status(a.task_id, ST_RUNNING)   # 运行中不计入等待
    e = reg.register("tts", "g3", user_id="u4", request_text="E")
    assert e is not None, "运行中不占等待名额，应能继续入队"

    # 3) 防重复：同用户+同会话+同请求，窗口内且仍在进行
    reg = make_registry(duplicate_window=120)
    r1 = reg.register("cover", "g1", user_id="u1", request_text="晴天")
    assert reg.find_duplicate("u1", "g1", "晴天") is not None
    assert reg.find_duplicate("u1", "g2", "晴天") is None, "不同会话不算重复"
    assert reg.find_duplicate("u2", "g1", "晴天") is None, "不同用户不算重复"
    assert reg.find_duplicate("u1", "g1", "雨爱") is None, "不同请求不算重复"
    reg.set_status(r1.task_id, ST_QUEUED)
    reg.set_status(r1.task_id, ST_RUNNING)
    reg.finish(r1, error="")
    assert r1.status == ST_COMPLETED
    assert reg.find_duplicate("u1", "g1", "晴天") is None, "任务结束后可再次提交"

    # 3b) received 直接收尾：不能标 completed，按失败/取消收尾（不卡等待名额）
    reg2 = make_registry(max_pending=1)
    rz = reg2.register("tts", "g9", user_id="u1", request_text="Z")
    reg2.finish(rz, error="异常")
    assert rz.status == ST_FAILED, "received 异常收尾应为 failed"
    ry = reg2.register("tts", "g9", user_id="u1", request_text="Y")
    assert ry is not None, "failed 记录不占等待名额"

    # 4) 状态流转合法性
    reg = make_registry()
    r = reg.register("tts", "g1", user_id="u1", request_text="X")
    assert r.status == ST_RECEIVED
    reg.set_status(r.task_id, ST_COMPLETED)      # received→completed 非法
    assert r.status == ST_RECEIVED, "非法流转应被拒绝"
    reg.set_status(r.task_id, ST_QUEUED)
    reg.set_status(r.task_id, ST_RUNNING)
    reg.set_status(r.task_id, ST_COMPLETED)      # running→completed 合法
    assert r.status == ST_COMPLETED
    reg.set_status(r.task_id, ST_RUNNING)        # 终态不可再变
    assert r.status == ST_COMPLETED

    # 5) 取消：排队任务立即取消；运行任务走 cancelling
    reg = make_registry()
    rq = reg.register("tts", "g1", user_id="u1", request_text="排队")
    reg.set_status(rq.task_id, ST_QUEUED)
    rr = reg.register("cover", "g1", user_id="u1", request_text="运行")
    reg.set_status(rr.task_id, ST_QUEUED)
    reg.set_status(rr.task_id, ST_RUNNING)
    ok, _ = reg.request_cancel(rq.task_id, by_user="u1")
    assert ok and rq.status == ST_CANCELLED
    ok, _ = reg.request_cancel(rr.task_id, by_user="u1")
    assert ok and rr.status == ST_CANCELLING
    reg.terminate_cancelled(rr.task_id)
    assert rr.status == ST_CANCELLED
    # 越权取消
    rx = reg.register("tts", "g2", user_id="u9", request_text="别人的")
    ok, msg = reg.request_cancel(rx.task_id, by_user="u1")
    assert not ok and rx.status == ST_RECEIVED
    # 管理员可以
    ok, _ = reg.request_cancel(rx.task_id, by_user="u1", is_admin=True)
    assert ok and rx.status == ST_CANCELLED

    # 6) 隐私视图：public_view 不含服务器路径与内部句柄
    reg = make_registry()
    r = reg.register("cover", "g1", user_id="u1", request_text="晴天")
    r.output_path = "/host/gateway_work/out/abc.wav"
    view = r.public_view()
    assert "output_path" not in view and "asyncio_task" not in view
    assert "/vol1" not in str(view)
    assert view["output_name"].endswith("abc.wav")

    # 7) 用户/超时/失败终态与清理
    reg = make_registry()
    r = reg.register("tts", "g1", user_id="u1", request_text="X")
    reg.set_status(r.task_id, ST_QUEUED)
    reg.set_status(r.task_id, ST_RUNNING)
    reg.finish_by_id(r.task_id, error="boom", timeout=True)
    assert r.status == ST_TIMEOUT and r.error == "boom"
    assert reg.user_tasks("u1", include_finished=True)

    # 8) 去重规范化：空白/标点/大小写
    from task_registry import normalize_request
    assert normalize_request("我想听你说，  晴天！") == normalize_request("我想听你说晴天")

    print("test_task_registry: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
