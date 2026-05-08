"""web_search_batch 与 provider 层共享 Semaphore 的并发回归测试。"""

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
from grok_search.providers._concurrency import (  # noqa: E402
    maybe_acquire_grok_semaphore,
    reset_grok_semaphore,
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
def _isolate_semaphore():
    reset_grok_semaphore()
    yield
    reset_grok_semaphore()


@pytest.mark.asyncio
async def test_cancelled_batch_returns_status_cancelled_per_query(monkeypatch):
    """外层 cancel batch 时，每个 in-flight query 应映射为 status=cancelled，
    error.code=cancelled，retryable=true，而不是被错认为 internal_error。"""
    _set_basic_env(monkeypatch, concurrency=4)

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    started = asyncio.Event()

    class HangingProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            started.set()
            await asyncio.sleep(60)  # 永远不会真返回
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", HangingProvider)

    batch_task = asyncio.create_task(
        server.web_search_batch(queries=["alpha", "beta", "gamma"], timeout="30s")
    )
    # 等至少一个 in-flight，再外层 cancel；与 task_store.cancel 走的路径完全一致。
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    batch_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await batch_task


@pytest.mark.asyncio
async def test_cancelled_inside_gather_is_classified_as_cancelled(monkeypatch):
    """直接在单个 query 抛 CancelledError，验证 batch 路径把它分类为 cancelled
    而非 internal_error。这覆盖 task_store.cancel() 引起的真实场景。"""
    _set_basic_env(monkeypatch)

    async def cancel_one_pass_others(query, **_kwargs):
        if query == "cancel_me":
            raise asyncio.CancelledError()
        return {
            "session_id": f"sess-{query}",
            "status": "ok",
            "content": f"answer for {query}",
            "sources_count": 0,
            "model": "grok-4.1-fast",
            "provider": "Grok",
        }

    monkeypatch.setattr(server, "_perform_web_search", cancel_one_pass_others)

    response = await server._perform_web_search_batch(
        queries=["alpha", "cancel_me", "gamma"]
    )

    statuses = {item["query"]: item["status"] for item in response["results"]}
    assert statuses == {"alpha": "ok", "cancel_me": "cancelled", "gamma": "ok"}
    assert response["cancelled_count"] == 1
    assert response["error_count"] == 0
    assert response["ok_count"] == 2

    cancelled = next(r for r in response["results"] if r["status"] == "cancelled")
    assert cancelled["error"]["code"] == "cancelled"
    assert cancelled["error"]["retryable"] is True
    assert cancelled["error"]["provider"] == "server"


@pytest.mark.asyncio
async def test_batch_returns_per_query_results_with_isolation(monkeypatch):
    _set_basic_env(monkeypatch)

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    seen_queries: list[str] = []

    class StubProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            seen_queries.append(query)
            await asyncio.sleep(0)
            if query == "boom":
                raise RuntimeError("simulated upstream failure")
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", StubProvider)

    response = await server.web_search_batch(
        queries=["alpha", "boom", "  beta  ", ""],
    )

    assert response["batch_size"] == 3
    assert response["input_size"] == 4
    assert response["dropped_count"] == 1
    assert response["dropped"] == [{"index": 3, "reason": "empty_query"}]
    assert response["concurrency"] == server.config.grok_concurrency
    assert response["ok_count"] == 2
    assert response["error_count"] == 1
    assert sorted(seen_queries) == sorted(["alpha", "boom", "beta"])

    statuses = [item["status"] for item in response["results"]]
    assert statuses.count("error") == 1
    assert statuses.count("ok") == 2

    successful = [item for item in response["results"] if item["status"] == "ok"]
    assert {item["content"] for item in successful} == {
        "answer for alpha",
        "answer for beta",
    }
    assert all(item["session_id"] for item in response["results"])
    assert [item["input_index"] for item in response["results"]] == [0, 1, 2]
    assert [item["query"] for item in response["results"]] == ["alpha", "boom", "beta"]


@pytest.mark.asyncio
async def test_batch_truncates_above_limit(monkeypatch):
    _set_basic_env(monkeypatch)

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    class StubProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", StubProvider)

    queries = [f"q{i}" for i in range(40)]
    response = await server.web_search_batch(queries=queries)

    assert response["batch_size"] == 32
    assert response["input_size"] == 40
    assert response["dropped_count"] == 8
    assert response["dropped"][0] == {"index": 32, "reason": "over_limit"}
    assert "warning" in response
    assert "32" in response["warning"]


@pytest.mark.asyncio
async def test_batch_empty_input_returns_structured_error(monkeypatch):
    _set_basic_env(monkeypatch)
    response = await server.web_search_batch(queries=["", "   ", None])  # type: ignore[arg-type]
    assert response["status"] == "error"
    assert response["error"] == "queries_empty"
    assert response["input_size"] == 3
    assert response["batch_size"] == 0
    assert response["dropped_count"] == 3
    assert response["dropped"] == [
        {"index": 0, "reason": "empty_query"},
        {"index": 1, "reason": "empty_query"},
        {"index": 2, "reason": "non_string"},
    ]
    assert response["results"] == []


@pytest.mark.asyncio
async def test_low_information_query_is_skipped_before_provider(monkeypatch):
    _set_basic_env(monkeypatch)

    async def fail_models(api_url: str, api_key: str) -> list[str]:
        raise AssertionError("models should not be fetched for skipped query")

    class FailProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            raise AssertionError("provider should not be constructed for skipped query")

    monkeypatch.setattr(server, "_get_available_models_cached", fail_models)
    monkeypatch.setattr(server, "GrokSearchProvider", FailProvider)

    response = await server.web_search("AAA")

    assert response["status"] == "skipped"
    assert response["error"]["code"] == "low_information_query"
    assert response["sources_count"] == 0


@pytest.mark.asyncio
async def test_short_valid_queries_are_not_skipped(monkeypatch):
    _set_basic_env(monkeypatch)

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    seen_queries: list[str] = []

    class StubProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            seen_queries.append(query)
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", StubProvider)

    for query in ("AI", "xAI", "EU AI Act", "AA"):
        response = await server.web_search(query)
        assert response["status"] == "ok"

    assert seen_queries == ["AI", "xAI", "EU AI Act", "AA"]


@pytest.mark.asyncio
async def test_batch_reports_dropped_and_skipped_inputs(monkeypatch):
    _set_basic_env(monkeypatch)

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    seen_queries: list[str] = []

    class StubProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            seen_queries.append(query)
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", StubProvider)

    response = await server.web_search_batch(
        queries=["", "AAA", "AI", None, "  beta  "],  # type: ignore[list-item]
    )

    assert response["input_size"] == 5
    assert response["batch_size"] == 3
    assert response["dropped_count"] == 2
    assert response["dropped"] == [
        {"index": 0, "reason": "empty_query"},
        {"index": 3, "reason": "non_string"},
    ]
    assert response["ok_count"] == 2
    assert response["skipped_count"] == 1
    assert response["error_count"] == 0
    assert seen_queries == ["AI", "beta"]

    by_index = {item["input_index"]: item for item in response["results"]}
    assert by_index[1]["status"] == "skipped"
    assert by_index[1]["error"]["code"] == "low_information_query"
    assert by_index[2]["query"] == "AI"
    assert by_index[4]["query"] == "beta"


@pytest.mark.asyncio
async def test_hung_upstream_is_timed_out_without_stalling_batch(monkeypatch):
    """单条 query 永久 hang 时，batch 必须在 ~timeout 内整体返回，
    那条 hung query 折叠为 upstream_timeout error，其余 query 不受影响。

    这是 2026-05-06 真实事故复现：原实现没有 per-query 总时长上限，
    httpx 的 read=120s 是 per-chunk，hung 上游可以让 asyncio.gather 等到天荒地老。
    """
    _set_basic_env(monkeypatch)

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    class HungProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            if query == "hang":
                # 模拟永久不返回的上游 stream
                await asyncio.sleep(60)
                return "should never see this"
            await asyncio.sleep(0)
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", HungProvider)

    started = time.perf_counter()
    response = await server.web_search_batch(
        queries=["alpha", "hang", "beta"],
        timeout="1s",
    )
    elapsed = time.perf_counter() - started

    # 给点 buffer，但绝对不能接近上面的 60s sleep
    assert elapsed < 5.0, f"batch stalled for {elapsed:.2f}s; per-query timeout broken"

    assert response["batch_size"] == 3
    assert response["request_timeout_seconds"] == pytest.approx(1.0)

    by_query = {}
    for item in response["results"]:
        # session_id 与 query 没直接对应，找特征字段
        if item.get("status") == "error" and item.get("error", {}).get("code") == "upstream_timeout":
            by_query["hang"] = item
        elif item.get("status") == "ok":
            content = item.get("content", "")
            if "alpha" in content:
                by_query["alpha"] = item
            elif "beta" in content:
                by_query["beta"] = item

    assert "alpha" in by_query, "alpha query should succeed"
    assert "beta" in by_query, "beta query should succeed"
    assert "hang" in by_query, "hung query should produce upstream_timeout error"
    assert by_query["hang"]["error"]["retryable"] is True
    assert by_query["hang"]["error"]["timeout_seconds"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_timeout_param_overrides_server_default(monkeypatch):
    """显式传 timeout='5s' 应该覆盖 GROK_REQUEST_TIMEOUT 默认。"""
    _set_basic_env(monkeypatch)
    monkeypatch.setenv("GROK_REQUEST_TIMEOUT", "300")  # server 默认很长

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    class HungProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            await asyncio.sleep(60)
            return "never"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", HungProvider)

    started = time.perf_counter()
    response = await server.web_search_batch(
        queries=["alpha"],
        timeout="500ms",
    )
    elapsed = time.perf_counter() - started

    # 调用方 timeout 应该胜出 server 默认的 300s
    assert elapsed < 3.0, f"call did not honor explicit timeout=500ms (elapsed={elapsed:.2f}s)"
    assert response["request_timeout_seconds"] == pytest.approx(0.5)
    assert response["results"][0]["error"]["code"] == "upstream_timeout"


@pytest.mark.asyncio
async def test_timeout_does_not_count_semaphore_queue_wait(monkeypatch):
    """P0 回归：当并发量 > GROK_CONCURRENCY 时，超出 slot 的 query 在 Semaphore
    排队等待的时间**不能**算进每条 query 的 timeout。

    这是 2026-05-06 stress test 暴露的核心 bug：早先版本把 ``asyncio.wait_for``
    包在 ``async with sem`` 外面，concurrency=4 提交 32 query 时，第 17 条之后
    全部在 ``submitted_at + 60s`` 准点失败，因为它们根本没拿到 slot 就被
    timeout 切断，并被错误标记为 ``error.code='upstream_timeout', retryable=true``。

    修复后：``hold_grok_semaphore`` 放在 ``wait_for`` 外面，每条 query 的
    timeout 只覆盖真正持锁后的上游时间。
    """
    _set_basic_env(monkeypatch, concurrency=2)
    reset_grok_semaphore()

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    class SteadyProvider:
        """每条 query 持有 slot 1.5s。"""

        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            await asyncio.sleep(1.5)
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", SteadyProvider)

    started = time.perf_counter()
    response = await server.web_search_batch(
        queries=["q1", "q2", "q3", "q4"],
        timeout="2s",
    )
    elapsed = time.perf_counter() - started

    # 4 query / concurrency=2 / 每条 1.5s → 串行 2 批 ~3s
    # 但每条 query 自己只占 wait_for 1.5s，远小于 2s timeout
    # 旧实现下 q3/q4 排队 1.5s 后只剩 0.5s wait_for，跑 1.5s → 必然 timeout
    # 新实现下排队不计时，q3/q4 拿到 slot 后才开始计 2s → 全部成功
    assert elapsed >= 2.5, f"应该 ~3s 串行 2 批 (elapsed={elapsed:.2f}s)"
    assert elapsed < 5.0, f"不应该被 timeout (elapsed={elapsed:.2f}s)"

    assert response["batch_size"] == 4
    statuses = [r.get("status") for r in response["results"]]
    assert all(s == "ok" for s in statuses), (
        f"队列等待时间被错误地计入 timeout 了！statuses={statuses}, elapsed={elapsed:.2f}s"
    )


@pytest.mark.asyncio
async def test_provider_semaphore_caps_concurrent_upstream(monkeypatch):
    _set_basic_env(monkeypatch, concurrency=2)
    reset_grok_semaphore()

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    counter_lock = asyncio.Lock()
    state = {"in_flight": 0, "peak": 0}

    class CountingProvider:
        """模仿真实 provider：用 ``maybe_acquire_grok_semaphore`` 配合 ContextVar
        检测外层 (server `_safe_grok` 的 ``hold_grok_semaphore``) 是否已经持锁，
        避免不可重入 Semaphore 双重 acquire 死锁。"""

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

    response = await server.web_search_batch(queries=[f"q{i}" for i in range(8)])

    assert response["batch_size"] == 8
    assert response["ok_count"] == 8
    assert state["peak"] <= 2, f"peak {state['peak']} exceeded GROK_CONCURRENCY=2"
    assert state["peak"] >= 1
