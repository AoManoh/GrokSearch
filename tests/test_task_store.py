"""TaskStore 与 submit/get/cancel/list 4 个 MCP 工具的回归测试。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from grok_search import server  # noqa: E402
from grok_search.providers._concurrency import reset_grok_semaphore  # noqa: E402
from grok_search.task_store import (  # noqa: E402
    TASK_STATE_CANCELLED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_RUNNING,
    parse_go_duration,
    reset_task_store,
)


def _set_basic_env(monkeypatch, *, concurrency: int | None = None) -> None:
    monkeypatch.setenv("GROK_API_URL", "https://example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "good-key")
    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "chat")
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    if concurrency is not None:
        monkeypatch.setenv("GROK_CONCURRENCY", str(concurrency))
    else:
        monkeypatch.delenv("GROK_CONCURRENCY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_globals():
    reset_grok_semaphore()
    reset_task_store()
    yield
    reset_grok_semaphore()
    reset_task_store()


def _install_stub_provider(monkeypatch, *, delay: float = 0.0, fail_query: str | None = None):
    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    class StubProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            if delay > 0:
                await asyncio.sleep(delay)
            if fail_query is not None and query == fail_query:
                raise RuntimeError("stub upstream failure")
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", StubProvider)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", 0.0),
        (None, 0.0),
        ("0s", 0.0),
        ("1s", 1.0),
        ("500ms", 0.5),
        ("2m", 120.0),
        ("4m", 240.0),
        ("garbage", 0.0),
        ("1h", 300.0),
        ("9999h", 300.0),
        ("-1s", 0.0),
        (1.5, 1.5),
    ],
)
def test_parse_go_duration_handles_common_inputs(value, expected):
    assert parse_go_duration(value) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_submit_returns_task_id_immediately(monkeypatch):
    _set_basic_env(monkeypatch)
    _install_stub_provider(monkeypatch, delay=0.05)

    started = time.perf_counter()
    snapshot = await server.submit_search_task(
        kind="web_search",
        params={"query": "alpha"},
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05, f"submit should return before stub finishes (elapsed={elapsed:.3f}s)"
    assert snapshot["task_id"].startswith("task-")
    assert snapshot["kind"] == "web_search"
    assert snapshot["state"] in {"queued", TASK_STATE_RUNNING}
    assert snapshot["params"] == {"query": "alpha"}


@pytest.mark.asyncio
async def test_submit_then_long_poll_returns_completed_result(monkeypatch):
    _set_basic_env(monkeypatch)
    _install_stub_provider(monkeypatch, delay=0.05)

    submit_snapshot = await server.submit_search_task(
        kind="web_search",
        params={"query": "alpha"},
    )
    task_id = submit_snapshot["task_id"]

    final_snapshot = await server.get_search_task_result(task_id=task_id, wait="2s")

    assert final_snapshot["state"] == TASK_STATE_COMPLETED
    assert final_snapshot["result"]["status"] == "ok"
    assert final_snapshot["result"]["content"] == "answer for alpha"
    assert final_snapshot["finished_at"] is not None


@pytest.mark.asyncio
async def test_get_with_zero_wait_returns_running_snapshot(monkeypatch):
    _set_basic_env(monkeypatch)
    _install_stub_provider(monkeypatch, delay=0.2)

    submit_snapshot = await server.submit_search_task(
        kind="web_search",
        params={"query": "alpha"},
    )
    task_id = submit_snapshot["task_id"]

    # 立刻读取，不等
    instant_snapshot = await server.get_search_task_result(task_id=task_id, wait="0s")
    assert instant_snapshot["state"] in {"queued", TASK_STATE_RUNNING}
    assert "result" not in instant_snapshot

    # 兜底等任务完成，避免污染下一条测试
    final_snapshot = await server.get_search_task_result(task_id=task_id, wait="2s")
    assert final_snapshot["state"] == TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_failed_task_records_error(monkeypatch):
    _set_basic_env(monkeypatch)
    _install_stub_provider(monkeypatch, delay=0.0, fail_query="boom")

    submit_snapshot = await server.submit_search_task(
        kind="web_search",
        params={"query": "boom"},
    )
    task_id = submit_snapshot["task_id"]

    final_snapshot = await server.get_search_task_result(task_id=task_id, wait="2s")

    # _perform_web_search 内部已经把上游异常包成 result["status"]="error"
    # 因此从 TaskStore 视角任务是 completed，error 信息在 result 里
    assert final_snapshot["state"] == TASK_STATE_COMPLETED
    assert final_snapshot["result"]["status"] == "error"


@pytest.mark.asyncio
async def test_cancel_running_task(monkeypatch):
    _set_basic_env(monkeypatch)
    _install_stub_provider(monkeypatch, delay=2.0)

    submit_snapshot = await server.submit_search_task(
        kind="web_search",
        params={"query": "long-running"},
    )
    task_id = submit_snapshot["task_id"]

    # 让 asyncio.create_task 实际进入 running
    await asyncio.sleep(0.05)

    cancel_snapshot = await server.cancel_search_task(task_id=task_id, hint="user")

    assert cancel_snapshot["state"] == TASK_STATE_CANCELLED
    assert cancel_snapshot["cancel_hint"] == "user"


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_error():
    response = await server.cancel_search_task(task_id="task-doesnotexist")
    assert response["status"] == "error"
    assert response["error"] == "task_not_found"


@pytest.mark.asyncio
async def test_get_unknown_task_returns_error():
    response = await server.get_search_task_result(task_id="task-doesnotexist", wait="0s")
    assert response["status"] == "error"
    assert response["error"] == "task_not_found"


@pytest.mark.asyncio
async def test_invalid_kind_rejected(monkeypatch):
    response = await server.submit_search_task(
        kind="not_a_real_kind",
        params={"query": "alpha"},
    )
    assert response["status"] == "error"
    assert response["error"] == "invalid_kind"


@pytest.mark.asyncio
async def test_web_search_missing_query_rejected(monkeypatch):
    response = await server.submit_search_task(
        kind="web_search",
        params={"platform": "GitHub"},
    )
    assert response["status"] == "error"
    assert response["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_web_search_batch_missing_queries_rejected(monkeypatch):
    response = await server.submit_search_task(
        kind="web_search_batch",
        params={"queries": ["", "  "]},
    )
    assert response["status"] == "error"
    assert response["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_list_tasks_with_filters(monkeypatch):
    _set_basic_env(monkeypatch)
    _install_stub_provider(monkeypatch, delay=0.05)

    a = await server.submit_search_task(kind="web_search", params={"query": "alpha"})
    b = await server.submit_search_task(kind="web_search", params={"query": "beta"})
    c = await server.submit_search_task(
        kind="web_search_batch",
        params={"queries": ["gamma1", "gamma2"]},
    )

    # 等所有任务跑完
    await server.get_search_task_result(task_id=a["task_id"], wait="2s")
    await server.get_search_task_result(task_id=b["task_id"], wait="2s")
    await server.get_search_task_result(task_id=c["task_id"], wait="2s")

    all_tasks = await server.list_search_tasks()
    assert all_tasks["count"] == 3

    only_batch = await server.list_search_tasks(kinds=["web_search_batch"])
    assert only_batch["count"] == 1
    assert only_batch["tasks"][0]["kind"] == "web_search_batch"

    only_completed = await server.list_search_tasks(states=[TASK_STATE_COMPLETED])
    assert only_completed["count"] == 3


@pytest.mark.asyncio
async def test_list_tasks_invalid_since_returns_error():
    response = await server.list_search_tasks(since="not-a-timestamp")
    assert response["status"] == "error"
    assert response["error"] == "invalid_since"


@pytest.mark.asyncio
async def test_concurrent_submits_respect_provider_semaphore(monkeypatch):
    _set_basic_env(monkeypatch, concurrency=2)
    reset_grok_semaphore()

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    counter_lock = asyncio.Lock()
    state = {"in_flight": 0, "peak": 0}

    from grok_search.providers._concurrency import maybe_acquire_grok_semaphore

    class CountingProvider:
        """Stub provider 用 ``maybe_acquire_grok_semaphore`` 复用 server 外层
        ``hold_grok_semaphore`` 已持有的 slot，避免双重 acquire 死锁。"""

        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            async with maybe_acquire_grok_semaphore():
                async with counter_lock:
                    state["in_flight"] += 1
                    if state["in_flight"] > state["peak"]:
                        state["peak"] = state["in_flight"]
                try:
                    await asyncio.sleep(0.05)
                    return f"answer for {query}"
                finally:
                    async with counter_lock:
                        state["in_flight"] -= 1

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", CountingProvider)

    submitted = []
    for i in range(8):
        snap = await server.submit_search_task(
            kind="web_search",
            params={"query": f"q{i}"},
        )
        submitted.append(snap["task_id"])

    # 等所有任务终态
    for tid in submitted:
        await server.get_search_task_result(task_id=tid, wait="3s")

    listing = await server.list_search_tasks()
    completed = [t for t in listing["tasks"] if t["state"] == TASK_STATE_COMPLETED]
    assert len(completed) == 8
    assert state["peak"] <= 2, f"semaphore breached: peak={state['peak']}"
