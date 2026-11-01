# -*- coding: utf-8 -*-
"""统一任务注册表（CUSTOM-8）：task_id 级的状态机与会话隔离。

为什么需要它：此前翻唱任务按会话（umo）存在 _active_cover_tasks 里——同群两个
用户共享一条记录，无法按用户查询/取消；TTS 只有队列没有任务记录。本模块提供：

  * 每个任务一条 TaskRecord（task_id / 会话 / 用户 / 类型 / 请求文本 / 状态 / 结果）；
  * 合法的状态流转（received→queued→running→completed，失败/取消/超时分支），
    非法流转会被拒绝并告警，终态不可再变更；
  * 防重复提交检测（同用户+同会话+相同请求，在时间窗口内且仍在进行中）；
  * 全局等待上限（received+queued 总数）；
  * 按用户的任务查询与取消（普通用户只能动自己的，管理员可指定任意任务）；
  * 队列位置计算（按提交时间 FIFO）。

只依赖标准库，可用假数据单独测试（tests/test_task_registry.py）。
线程/协程安全说明：AstrBot 单事件循环内使用，不做跨进程锁；
Gateway 层的串行由网关自身的全局信号量保证（单实例部署）。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 状态常量
ST_RECEIVED = "received"
ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_CANCELLING = "cancelling"
ST_COMPLETED = "completed"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"
ST_TIMEOUT = "timeout"
ST_REJECTED = "rejected"

TERMINAL_STATES = {ST_COMPLETED, ST_FAILED, ST_CANCELLED, ST_TIMEOUT, ST_REJECTED}

# 合法流转（缺省一律非法，防止散落的回调把状态改乱）
_VALID_TRANSITIONS: Dict[str, tuple] = {
    ST_RECEIVED: (ST_QUEUED, ST_RUNNING, ST_REJECTED, ST_CANCELLED, ST_FAILED),
    ST_QUEUED: (ST_RUNNING, ST_CANCELLED, ST_FAILED),
    ST_RUNNING: (ST_COMPLETED, ST_FAILED, ST_CANCELLING, ST_TIMEOUT),
    ST_CANCELLING: (ST_CANCELLED, ST_FAILED),
}

_ACTIVE_STATES = {ST_RECEIVED, ST_QUEUED, ST_RUNNING, ST_CANCELLING}

# 防重复比较用的规范化：去空白与常见标点，小写
_NORMALIZE_RE = re.compile(r"[\s，。！!？?、：:「」『』\"'·…]+")


def normalize_request(text: Any) -> str:
    return _NORMALIZE_RE.sub("", str(text or "")).casefold()


@dataclass
class TaskRecord:
    task_id: str
    task_type: str                 # "cover" | "tts"
    conversation_id: str           # unified_msg_origin
    group_id: str = ""
    user_id: str = ""
    user_name: str = ""
    message_id: str = ""
    request_text: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = ST_RECEIVED
    gateway_task_id: str = ""
    output_path: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    # 运行时句柄（不参与序列化比较）
    cancel_requested: bool = False
    asyncio_task: Any = None
    gradio_job: Any = None

    @property
    def active(self) -> bool:
        return self.status in _ACTIVE_STATES

    def public_view(self) -> dict:
        """对外安全视图：不含内部句柄与服务器路径。"""
        return {
            "task_id": self.task_id,
            "type": self.task_type,
            "status": self.status,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "request": self.request_text[:60],
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error[:120],
            "output_name": (self.output_path.rsplit("/", 1)[-1]
                            if self.output_path else ""),
        }


class TaskRegistry:
    """task_id → TaskRecord 的统一注册表。"""

    def __init__(self, max_pending: int = 10, max_finished: int = 200,
                 duplicate_window: float = 120.0):
        self._tasks: Dict[str, TaskRecord] = {}
        self._order: List[str] = []          # 按提交顺序，FIFO 依据
        self._lock = threading.Lock()
        self.max_pending = max(1, int(max_pending))
        self.max_finished = max(10, int(max_finished))
        self.duplicate_window = max(0.0, float(duplicate_window))

    # ---------------- 注册与查询 ----------------

    def register(self, task_type: str, conversation_id: str, user_id: str = "",
                 user_name: str = "", group_id: str = "", message_id: str = "",
                 request_text: str = "") -> Optional[TaskRecord]:
        """登记新任务。等待数（received+queued）达到上限时返回 None（调用方拒绝）。"""
        with self._lock:
            pending = sum(1 for t in self._tasks.values() if t.status in (ST_RECEIVED, ST_QUEUED))
            if pending >= self.max_pending:
                return None
            task_id = uuid.uuid4().hex[:12]
            rec = TaskRecord(
                task_id=task_id,
                task_type=str(task_type or "cover"),
                conversation_id=str(conversation_id or ""),
                group_id=str(group_id or ""),
                user_id=str(user_id or ""),
                user_name=str(user_name or ""),
                message_id=str(message_id or ""),
                request_text=str(request_text or "")[:200],
            )
            self._tasks[task_id] = rec
            self._order.append(task_id)
            self._trim_locked()
            return rec

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(str(task_id or ""))

    def find_duplicate(self, user_id: str, conversation_id: str,
                       request_text: str) -> Optional[TaskRecord]:
        """短窗口重复提交检测：同用户+同会话+规范化后相同请求，且任务仍在进行中。"""
        needle = normalize_request(request_text)
        if not needle:
            return None
        now = time.time()
        with self._lock:
            for tid in reversed(self._order):
                rec = self._tasks.get(tid)
                if not rec or not rec.active:
                    continue
                if rec.user_id != str(user_id) or rec.conversation_id != str(conversation_id):
                    continue
                if normalize_request(rec.request_text) != needle:
                    continue
                if self.duplicate_window > 0 and now - rec.created_at > self.duplicate_window:
                    continue
                return rec
        return None

    def queue_position(self, task_id: str) -> int:
        """任务在等待队列中的位置（1 起）；不在排队态返回 0。"""
        with self._lock:
            queued = [tid for tid in self._order
                      if self._tasks.get(tid) and self._tasks[tid].status == ST_QUEUED]
            try:
                return queued.index(str(task_id)) + 1
            except ValueError:
                return 0

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status in (ST_RECEIVED, ST_QUEUED))

    def user_tasks(self, user_id: str, include_finished: bool = False) -> List[TaskRecord]:
        with self._lock:
            recs = [self._tasks[tid] for tid in reversed(self._order)
                    if self._tasks[tid].user_id == str(user_id)]
        if not include_finished:
            recs = [r for r in recs if r.active]
        return recs

    def all_tasks(self) -> List[TaskRecord]:
        with self._lock:
            return [self._tasks[tid] for tid in reversed(self._order)]

    # ---------------- 状态流转（唯一入口） ----------------

    def set_status(self, task_id: str, status: str, error: str = "") -> Optional[TaskRecord]:
        rec = self._tasks.get(str(task_id or ""))
        if rec is None:
            return None
        with self._lock:
            if rec.status in TERMINAL_STATES:
                if status != rec.status:
                    # 终态不可再变；并发下的重复终结（如超时与失败竞态）以先到为准
                    return rec
            if status not in _VALID_TRANSITIONS.get(rec.status, ()):
                return rec  # 非法流转：保持原状态（调用方日志自见）
            rec.status = status
            if status == ST_RUNNING and not rec.started_at:
                rec.started_at = time.time()
            if status in TERMINAL_STATES:
                rec.finished_at = time.time()
                if error:
                    rec.error = str(error)[:300]
        return rec

    def begin(self, rec: TaskRecord) -> TaskRecord:
        """received/queued → queued → running 的便捷入口（排队位置从进入即固定）。"""
        if rec.status == ST_RECEIVED:
            self.set_status(rec.task_id, ST_QUEUED)
        return self.set_status(rec.task_id, ST_RUNNING)

    def finish(self, rec: TaskRecord, error: str = "", timeout: bool = False) -> TaskRecord:
        if rec.status == ST_RECEIVED:
            # 从未进入排队/运行的记录不能标记 completed：按失败/取消收尾，
            # 避免非法流转被拒后记录永远卡在活跃态、占用等待名额
            target = ST_FAILED if (error or timeout) else ST_CANCELLED
            return self.set_status(rec.task_id, target, error=error)
        target = ST_TIMEOUT if timeout else (ST_FAILED if error else ST_COMPLETED)
        return self.set_status(rec.task_id, target, error=error)

    def finish_by_id(self, task_id: str, error: str = "", timeout: bool = False) -> Optional[TaskRecord]:
        rec = self.get(task_id)
        if rec is None:
            return None
        return self.finish(rec, error=error, timeout=timeout)

    def begin_by_id(self, task_id: str) -> Optional[TaskRecord]:
        """queued/received → running（开始执行时调用）。"""
        rec = self.get(task_id)
        if rec is None:
            return None
        return self.begin(rec)

    def terminate_cancelled(self, task_id: str) -> Optional[TaskRecord]:
        """把取消中的任务落成 cancelled 终态（running → cancelling → cancelled）。"""
        rec = self.get(task_id)
        if rec is None:
            return None
        if rec.status == ST_RUNNING:
            self.set_status(task_id, ST_CANCELLING)
        if rec.status == ST_CANCELLING or rec.status == ST_RECEIVED:
            self.set_status(task_id, ST_CANCELLED)
        return self.get(task_id)

    def request_cancel(self, task_id: str, by_user: str = "",
                       is_admin: bool = False) -> tuple[bool, str]:
        """请求取消。排队/已接收任务立即置 cancelled；运行中置 cancelling（由执行方终止）。"""
        rec = self._tasks.get(str(task_id or ""))
        if rec is None:
            return False, "任务不存在"
        if not is_admin and rec.user_id != str(by_user):
            return False, "只能取消自己的任务"
        if rec.status in TERMINAL_STATES:
            return False, f"任务已结束（{rec.status}）"
        rec.cancel_requested = True
        if rec.status in (ST_RECEIVED, ST_QUEUED):
            self.set_status(rec.task_id, ST_CANCELLED)
            return True, "已取消排队任务"
        self.set_status(rec.task_id, ST_CANCELLING)
        return True, "正在取消运行中的任务"

    # ---------------- 清理 ----------------

    def _trim_locked(self) -> None:
        finished = [tid for tid in self._order
                    if self._tasks[tid].status in TERMINAL_STATES]
        excess = len(finished) - self.max_finished
        for tid in finished[:max(0, excess)]:
            self._tasks.pop(tid, None)
            try:
                self._order.remove(tid)
            except ValueError:
                pass
