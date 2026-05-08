"""本地压力回归（不依赖真实 grok2api）：

用 fake provider 模拟典型故障矩阵，验证 7 次 commit 的核心修复在大并发下
不会引入新的回归：

1. concurrency=4 + 32 query batch → 不出现"假阳性 upstream_timeout"。
2. 混合成功 / 失败 / 取消，统计 ok/error/cancelled_count 与 results 一致。
3. 32 query auto-async 自动转后台任务，立即返回 task_id，后续可拿最终 payload。
4. extra_sources 失败时 warning + extra_failures 显式暴露，不静默吞噬。
"""

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
    TASK_STATE_COMPLETED,
    reset_task_store,
)


@pytest.fixture(autouse=True)
def _isolate_globals():
    reset_grok_semaphore()
    reset_task_store()
    yield
    reset_grok_semaphore()
    reset_task_store()


def _set_basic_env(monkeypatch, *, concurrency: int = 4) -> None:
    monkeypatch.setenv("GROK_API_URL", "https://example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "good-key")
    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "chat")
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setenv("GROK_CONCURRENCY", str(concurrency))


def _install_steady_provider(monkeypatch, *, delay: float = 0.1):
    async def fake_models(_url: str, _key: str) -> list[str]:
        return ["grok-4.1-fast"]

    class SteadyProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            await asyncio.sleep(delay)
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", SteadyProvider)


@pytest.mark.asyncio
async def test_stress_32_queries_no_false_timeouts(monkeypatch):
    """concurrency=4 + 32 query batch，每条上游 0.2s。
    串行下界 = 32 / 4 * 0.2 = 1.6s。每条 timeout=10s 远大于排队时间，
    所有 query 都应成功；P0 修复保证排队时间不算 timeout。
    """
    _set_basic_env(monkeypatch, concurrency=4)
    _install_steady_provider(monkeypatch, delay=0.2)

    queries = [f"q{i}" for i in range(32)]
    started = time.perf_counter()
    response = await server.web_search_batch(
        queries=queries,
        timeout="10s",
        auto_async_threshold=0,  # 强制同步，验证不挂
    )
    elapsed = time.perf_counter() - started

    assert response["batch_size"] == 32
    assert response["ok_count"] == 32, response
    assert response["error_count"] == 0
    assert response["cancelled_count"] == 0
    # 串行下界 1.6s，加调度开销允许 5s
    assert 1.5 < elapsed < 5.0, f"expected 32q@conc4 ≈ 1.6-3s, got {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_stress_mixed_success_failure_cancellation(monkeypatch):
    """20 query 混合：3 个抛错、5 个延迟到 timeout、其他成功。
    验证统计字段正确：ok_count + error_count = 20，分类清晰。
    """
    _set_basic_env(monkeypatch, concurrency=4)

    async def fake_models(_url: str, _key: str) -> list[str]:
        return ["grok-4.1-fast"]

    class MixedProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            if query.startswith("err_"):
                raise RuntimeError(f"simulated upstream failure for {query}")
            if query.startswith("hang_"):
                await asyncio.sleep(60)  # 永远不返回，会被 timeout 切
            await asyncio.sleep(0.1)
            return f"answer for {query}"

        def get_provider_name(self) -> str:
            return "Grok"

    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", MixedProvider)

    queries = (
        [f"err_{i}" for i in range(3)]
        + [f"hang_{i}" for i in range(5)]
        + [f"ok_{i}" for i in range(12)]
    )

    response = await server.web_search_batch(
        queries=queries,
        timeout="2s",
        auto_async_threshold=0,
    )

    assert response["batch_size"] == 20
    assert response["ok_count"] == 12
    # 3 个抛错 + 5 个 hang 都应被分类为 error（其中 hang 是 upstream_timeout）
    assert response["error_count"] == 8
    assert response["cancelled_count"] == 0
    # 检查 hang query 的错误码
    hang_results = [r for r in response["results"] if r["query"].startswith("hang_")]
    assert all(r.get("error", {}).get("code") == "upstream_timeout" for r in hang_results)
    assert all(r.get("error", {}).get("retryable") is True for r in hang_results)


@pytest.mark.asyncio
async def test_stress_auto_async_returns_immediately_for_long_batch(monkeypatch):
    """auto_async_threshold=8 + 16 query batch 应立即返回 task_id。

    模拟 AI 提交长 batch 不阻塞主调用通道的体验。
    """
    _set_basic_env(monkeypatch, concurrency=4)
    _install_steady_provider(monkeypatch, delay=0.5)

    queries = [f"q{i}" for i in range(16)]
    started = time.perf_counter()
    response = await server.web_search_batch(
        queries=queries,
        timeout="60s",
        auto_async_threshold=8,
    )
    elapsed_submit = time.perf_counter() - started

    assert response["status"] == "submitted"
    assert response["batch_size"] == 16
    assert elapsed_submit < 0.3, f"submit should be near-instant, got {elapsed_submit:.2f}s"

    task_id = response["task_id"]
    final = await server.get_search_task_result(task_id=task_id, wait="10s")
    assert final["state"] == TASK_STATE_COMPLETED
    assert final["result"]["ok_count"] == 16


@pytest.mark.asyncio
async def test_stress_extra_source_failures_surface_under_load(monkeypatch):
    """8 query batch + tavily 全部 401，验证每条 query 都收到 extra_failures。"""
    _set_basic_env(monkeypatch, concurrency=4)
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "tav-key")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    _install_steady_provider(monkeypatch, delay=0.05)

    async def boom_tavily(_query, _max):
        raise RuntimeError("tavily 401 unauthorized")

    monkeypatch.setattr(server, "_call_tavily_search", boom_tavily)

    response = await server.web_search_batch(
        queries=[f"q{i}" for i in range(8)],
        extra_sources=3,
        timeout="10s",
        auto_async_threshold=0,
    )

    assert response["ok_count"] == 8
    for item in response["results"]:
        assert item.get("extra_failures"), f"expected extra_failures for {item['query']}"
        assert item["extra_failures"][0]["provider"] == "tavily"
        assert "tavily 401 unauthorized" in item["extra_failures"][0]["message"]
        assert "tavily" in item.get("warning", "")
