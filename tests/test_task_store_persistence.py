"""TaskStore JSONL 持久化与启动 replay 回归测试。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from grok_search.task_store import (  # noqa: E402
    TASK_STATE_CANCELLED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TaskStore,
)


@pytest.mark.asyncio
async def test_complete_task_writes_submit_start_complete_lines(tmp_path):
    """成功任务应在 JSONL 里产生 submit / start / complete 三行事件。"""
    storage = tmp_path / "tasks.jsonl"
    store = TaskStore(storage_path=storage)

    async def runner():
        return {"answer": "ok"}

    record = await store.submit("web_search", {"query": "hello"}, runner)
    await record._asyncio_task

    lines = [json.loads(line) for line in storage.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [l["event"] for l in lines] == ["submit", "start", "complete"]
    assert lines[0]["task_id"] == record.task_id
    assert lines[0]["params"] == {"query": "hello"}
    assert lines[2]["result"] == {"answer": "ok"}


@pytest.mark.asyncio
async def test_failed_task_writes_fail_event(tmp_path):
    storage = tmp_path / "tasks.jsonl"
    store = TaskStore(storage_path=storage)

    async def boom():
        raise RuntimeError("upstream 500")

    record = await store.submit("web_search", {"query": "hello"}, boom)
    await record._asyncio_task

    events = [json.loads(line) for line in storage.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [e["event"] for e in events] == ["submit", "start", "fail"]
    assert "RuntimeError: upstream 500" in events[2]["error"]


@pytest.mark.asyncio
async def test_cancelled_task_writes_cancel_event(tmp_path):
    storage = tmp_path / "tasks.jsonl"
    store = TaskStore(storage_path=storage)

    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(60)
        return {"never": True}

    record = await store.submit("web_search", {"query": "slow"}, slow)
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await store.cancel(record.task_id, hint="test")

    events = [json.loads(line) for line in storage.read_text(encoding="utf-8").splitlines() if line.strip()]
    event_types = [e["event"] for e in events]
    assert event_types[0] == "submit"
    assert event_types[-1] == "cancel"
    assert events[-1]["cancel_hint"] == "test"


@pytest.mark.asyncio
async def test_replay_restores_completed_task(tmp_path):
    """重启后 completed 任务应原样可见。"""
    storage = tmp_path / "tasks.jsonl"
    store = TaskStore(storage_path=storage)

    async def runner():
        return {"answer": "persist"}

    record = await store.submit("web_search", {"query": "x"}, runner)
    await record._asyncio_task
    saved_id = record.task_id

    # 模拟"重启"：丢弃旧 store，新建一个指向同一个 storage_path 的实例
    new_store = TaskStore(storage_path=storage)
    restored = await new_store.get(saved_id)
    assert restored is not None
    assert restored.state == TASK_STATE_COMPLETED
    assert restored.result == {"answer": "persist"}


@pytest.mark.asyncio
async def test_replay_marks_in_flight_task_as_lost(tmp_path):
    """只有 submit + start 但缺 terminal 事件的任务，replay 后应标记为 failed
    + error='lost on restart'，避免轮询者卡死。"""
    storage = tmp_path / "tasks.jsonl"
    storage.write_text(
        "\n".join([
            json.dumps({"event": "submit", "task_id": "task-zombie", "kind": "web_search",
                        "params": {"query": "lost"}, "submitted_at": time.time()}),
            json.dumps({"event": "start", "task_id": "task-zombie", "started_at": time.time()}),
        ]) + "\n",
        encoding="utf-8",
    )

    new_store = TaskStore(storage_path=storage)
    restored = await new_store.get("task-zombie")
    assert restored is not None
    assert restored.state == TASK_STATE_FAILED
    assert restored.error == "lost on restart"


@pytest.mark.asyncio
async def test_replay_skips_corrupt_lines(tmp_path):
    """JSONL 中夹杂损坏行不应让整次 replay 崩溃，只跳过坏行。"""
    storage = tmp_path / "tasks.jsonl"
    storage.write_text(
        "\n".join([
            json.dumps({"event": "submit", "task_id": "task-good", "kind": "web_search",
                        "params": {}, "submitted_at": time.time()}),
            "{ this is not json",
            json.dumps({"event": "complete", "task_id": "task-good", "finished_at": time.time(),
                        "result": {"ok": True}}),
        ]) + "\n",
        encoding="utf-8",
    )

    new_store = TaskStore(storage_path=storage)
    restored = await new_store.get("task-good")
    assert restored is not None
    assert restored.state == TASK_STATE_COMPLETED
    assert restored.result == {"ok": True}


@pytest.mark.asyncio
async def test_persistence_disabled_falls_back_to_memory(tmp_path, monkeypatch):
    """storage_path 写入失败时降级为纯内存模式，任务仍能正常运行。"""
    storage = tmp_path / "tasks.jsonl"
    store = TaskStore(storage_path=storage)

    # 模拟磁盘写失败：把 _append_event 内部的 open 替换为抛 OSError
    original_open = Path.open

    def _broken_open(self, *args, **kwargs):
        if self == storage and args and args[0] == "a":
            raise OSError("disk full")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _broken_open)

    async def runner():
        return {"answer": "ok"}

    record = await store.submit("web_search", {"query": "hello"}, runner)
    await record._asyncio_task
    assert record.state == TASK_STATE_COMPLETED
    assert record.result == {"answer": "ok"}
    # 一旦失败应自动 disable 后续写入
    assert store._persistence_disabled is True
