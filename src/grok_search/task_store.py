"""异步任务存储与生命周期管理。

为 ``submit_search_task`` / ``get_search_task_result`` / ``cancel_search_task`` /
``list_search_tasks`` 4 个 MCP 工具提供进程级共享存储。

设计要点：

- 单进程内存版，使用 ``OrderedDict`` 维护 LRU 顺序；不做磁盘持久化。
- 256 条软上限：超出时优先淘汰已 terminal 的任务，保留进行中的任务。
- 状态机：``queued -> running -> completed | failed | cancelled``。
- 实际搜索仍走原同步路径（``_perform_web_search`` 等），底层并发受
  ``GROK_CONCURRENCY`` Semaphore 限流，不做二次 worker pool。
- 取消语义：调用 ``asyncio.Task.cancel()``，让正在等 Semaphore 或正在 stream
  的任务退出；终态任务返回当前快照不动。
- ``parse_go_duration`` 兼容 Go 风格 ``"30s" / "2m" / "500ms" / "1h"``，封顶 5 分钟，
  解析失败返回 0（即立刻读快照），保证 long-poll 不会因为客户端误传字符串卡住。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


_DURATION_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_MAX_WAIT_SECONDS = 300.0  # 5 分钟
_MAX_TASKS = 256

TASK_STATE_QUEUED = "queued"
TASK_STATE_RUNNING = "running"
TASK_STATE_COMPLETED = "completed"
TASK_STATE_FAILED = "failed"
TASK_STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_CANCELLED})

VALID_KINDS = frozenset({"web_search", "web_search_batch"})


def parse_go_duration(value: str, *, max_seconds: float | None = None) -> float:
    """把 Go 风格 duration 字符串解析为秒。

    参数：
    - ``value``: 字符串如 ``"30s"`` / ``"2m"`` / ``"500ms"`` / ``"1h"``；也可传数值。
      空字符串、None、无法解析的输入一律返回 0（调用方据此走默认）。
    - ``max_seconds``: 调用方指定的安全上限。``None`` 时使用模块默认 5 分钟。
      不同工具的合理上限不同——任务长轮询 5 分钟够用，单次上游请求超时
      可放宽到 10 分钟，所以由调用方按场景指定。
    """
    cap = max_seconds if max_seconds is not None else _MAX_WAIT_SECONDS
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        if not text:
            return 0.0
        match = _DURATION_PATTERN.match(text)
        if not match:
            return 0.0
        amount = float(match.group(1))
        unit = (match.group(2) or "s").lower()
        seconds = {
            "ms": amount / 1000.0,
            "s": amount,
            "m": amount * 60.0,
            "h": amount * 3600.0,
        }.get(unit, amount)
    if seconds < 0:
        return 0.0
    if seconds > cap:
        return cap
    return seconds


def _new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:16]}"


def _utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class TaskRecord:
    task_id: str
    kind: str
    params: dict[str, Any]
    state: str = TASK_STATE_QUEUED
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_hint: str | None = None
    _asyncio_task: asyncio.Task | None = None
    _done_event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_public_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "kind": self.kind,
            "state": self.state,
            "submitted_at": _utc_iso(self.submitted_at),
            "started_at": _utc_iso(self.started_at),
            "finished_at": _utc_iso(self.finished_at),
            "params": self.params,
        }
        if self.result is not None:
            out["result"] = self.result
        if self.error:
            out["error"] = self.error
        if self.cancel_hint:
            out["cancel_hint"] = self.cancel_hint
        return out

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class TaskStore:
    """进程级异步搜索任务存储。"""

    def __init__(self, max_tasks: int = _MAX_TASKS):
        self._max_tasks = max_tasks
        self._lock = asyncio.Lock()
        self._records: OrderedDict[str, TaskRecord] = OrderedDict()

    async def submit(
        self,
        kind: str,
        params: dict[str, Any],
        runner,
    ) -> TaskRecord:
        """创建任务并通过 ``asyncio.create_task`` 立即调度执行。

        ``runner`` 是一个无参 async 可调用，由 server 层封装好对应的
        ``_perform_web_search`` 或 ``web_search_batch`` 调用。
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid task kind: {kind!r}; expected one of {sorted(VALID_KINDS)}")

        record = TaskRecord(
            task_id=_new_task_id(),
            kind=kind,
            params=params,
        )
        async with self._lock:
            self._records[record.task_id] = record
            self._records.move_to_end(record.task_id)
            self._evict_locked()

        async def _wrapper():
            record.started_at = time.time()
            record.state = TASK_STATE_RUNNING
            try:
                result = await runner()
            except asyncio.CancelledError:
                record.state = TASK_STATE_CANCELLED
                record.finished_at = time.time()
                if not record.cancel_hint:
                    record.cancel_hint = "cancelled"
                record._done_event.set()
                raise
            except Exception as exc:
                record.state = TASK_STATE_FAILED
                record.error = f"{type(exc).__name__}: {exc}"
                record.finished_at = time.time()
                record._done_event.set()
                return
            else:
                record.state = TASK_STATE_COMPLETED
                record.result = result if isinstance(result, dict) else {"value": result}
                record.finished_at = time.time()
                record._done_event.set()

        record._asyncio_task = asyncio.create_task(_wrapper(), name=record.task_id)
        return record

    async def get(self, task_id: str, wait_seconds: float = 0.0) -> TaskRecord | None:
        record = await self._peek(task_id)
        if record is None:
            return None
        if wait_seconds <= 0 or record.is_terminal:
            return record
        try:
            await asyncio.wait_for(record._done_event.wait(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            pass
        return await self._peek(task_id)

    async def cancel(self, task_id: str, hint: str | None = None) -> TaskRecord | None:
        record = await self._peek(task_id)
        if record is None:
            return None
        if record.is_terminal:
            return record

        record.cancel_hint = (hint or "client").strip() or "client"
        task = record._asyncio_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(record._done_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                # _wrapper 还没观察到 CancelledError，强行打标签便于客户端立刻拿到反馈
                if not record.is_terminal:
                    record.state = TASK_STATE_CANCELLED
                    record.finished_at = time.time()
                    record._done_event.set()
        else:
            if not record.is_terminal:
                record.state = TASK_STATE_CANCELLED
                record.finished_at = time.time()
                record._done_event.set()
        return record

    async def list_tasks(
        self,
        states: list[str] | None = None,
        kinds: list[str] | None = None,
        since: float | None = None,
    ) -> list[TaskRecord]:
        async with self._lock:
            records = list(self._records.values())
        records.sort(key=lambda r: r.submitted_at)
        if states:
            wanted_states = {s for s in states if s}
            records = [r for r in records if r.state in wanted_states]
        if kinds:
            wanted_kinds = {k for k in kinds if k}
            records = [r for r in records if r.kind in wanted_kinds]
        if since is not None:
            records = [r for r in records if r.submitted_at >= since]
        return records

    async def _peek(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            record = self._records.get(task_id)
            if record is not None:
                self._records.move_to_end(record.task_id)
            return record

    def _evict_locked(self) -> None:
        """在 ``self._lock`` 持有的前提下淘汰超过容量的旧任务。"""
        if len(self._records) <= self._max_tasks:
            return
        # 优先淘汰最早的 terminal 任务，保留 running/queued
        terminal_ids = [
            tid for tid, rec in self._records.items() if rec.is_terminal
        ]
        for tid in terminal_ids:
            if len(self._records) <= self._max_tasks:
                return
            self._records.pop(tid, None)
        # 仍超出说明同时存在 256+ 进行中任务，按 FIFO 强淘汰
        while len(self._records) > self._max_tasks:
            self._records.popitem(last=False)


_STORE: TaskStore | None = None


def get_task_store() -> TaskStore:
    global _STORE
    if _STORE is None:
        _STORE = TaskStore()
    return _STORE


def reset_task_store() -> None:
    """重置全局 TaskStore，仅供测试或显式重新加载使用。"""
    global _STORE
    _STORE = None


__all__ = [
    "TASK_STATE_QUEUED",
    "TASK_STATE_RUNNING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELLED",
    "TERMINAL_STATES",
    "VALID_KINDS",
    "TaskRecord",
    "TaskStore",
    "get_task_store",
    "parse_go_duration",
    "reset_task_store",
]
