import json
import sys
import time
import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from grok_search import server  # noqa: E402


@pytest.mark.asyncio
async def test_web_fetch_falls_back_to_grok(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "http://example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    async def fake_tavily(_url):
        return None

    async def fake_firecrawl(_url, _ctx=None):
        return None

    class FakeProvider:
        async def fetch(self, url, ctx=None):
            return f"# fetched\n\n{url}"

    monkeypatch.setattr(server, "_call_tavily_extract", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_scrape", fake_firecrawl)
    monkeypatch.setattr(server, "_build_grok_provider", lambda model="": FakeProvider())

    result = await server.web_fetch("https://example.com/article")
    assert "# fetched" in result
    assert "https://example.com/article" in result


@pytest.mark.asyncio
async def test_web_fetch_timeout_cuts_off_grok_fetch(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "http://example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    async def fake_tavily(_url):
        return None

    async def fake_firecrawl(_url, _ctx=None):
        return None

    class SlowProvider:
        async def fetch(self, url, ctx=None):
            await asyncio.sleep(60)
            return f"# fetched\n\n{url}"

    async def fail_basic_fetch(_url, timeout=30.0):
        raise AssertionError("basic fallback should not run after total fetch timeout")

    monkeypatch.setattr(server, "_call_tavily_extract", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_scrape", fake_firecrawl)
    monkeypatch.setattr(server, "_build_grok_provider", lambda model="": SlowProvider())
    monkeypatch.setattr(server, "_call_basic_http_fetch", fail_basic_fetch)

    started = time.perf_counter()
    result = await server.web_fetch("https://example.com/article", timeout="500ms")
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0
    assert "提取超时" in result
    assert "0.5s" in result


class _FakeOkGrokProvider:
    """固定返回成功的 Grok provider stub，用于 extra_sources 失败路径测试。"""

    def __init__(self, *_args, **_kwargs):
        pass

    async def search(self, query, platform=""):
        return f"primary answer for {query}"

    async def search_response(self, query, platform=""):
        return server.SearchResponse(
            answer=f"primary answer for {query}",
            sources=[],
            raw_content=f"primary answer for {query}",
            provider="Grok",
            model="grok-4.1-fast",
        )

    def get_provider_name(self):
        return "Grok"


async def _fake_models_ok(_url, _key):
    return ["grok-4.1-fast"]


@pytest.mark.asyncio
async def test_tavily_failure_is_surfaced_as_warning(monkeypatch):
    """tavily 抛异常时应作为 warning + extra_failures 暴露，不再静默吞噬。"""
    monkeypatch.setenv("GROK_API_URL", "http://example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tav-key")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    async def boom_tavily(_query, _max):
        raise RuntimeError("tavily 401 unauthorized")

    monkeypatch.setattr(server, "_get_available_models_cached", _fake_models_ok)
    monkeypatch.setattr(server, "GrokSearchProvider", _FakeOkGrokProvider)
    monkeypatch.setattr(server, "_call_tavily_search", boom_tavily)

    result = await server._perform_web_search("hello world", extra_sources=3)

    assert result["status"] == "ok", result
    assert "primary answer" in result["content"]
    assert result.get("extra_failures"), "extra_failures 未透传到响应"
    failure = result["extra_failures"][0]
    assert failure["provider"] == "tavily"
    assert failure["type"] == "RuntimeError"
    assert "tavily 401 unauthorized" in failure["message"]
    assert "额外信源失败" in result.get("warning", "")
    assert "tavily" in result["warning"]


@pytest.mark.asyncio
async def test_firecrawl_failure_is_surfaced_as_warning(monkeypatch):
    """firecrawl 抛异常时应作为 warning + extra_failures 暴露，不再静默吞噬。"""
    monkeypatch.setenv("GROK_API_URL", "http://example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")

    async def boom_firecrawl(_query, _limit):
        raise TimeoutError("firecrawl read timeout")

    monkeypatch.setattr(server, "_get_available_models_cached", _fake_models_ok)
    monkeypatch.setattr(server, "GrokSearchProvider", _FakeOkGrokProvider)
    monkeypatch.setattr(server, "_call_firecrawl_search", boom_firecrawl)

    result = await server._perform_web_search("hello world", extra_sources=2)

    assert result["status"] == "ok", result
    assert result.get("extra_failures"), "extra_failures 未透传到响应"
    failure = result["extra_failures"][0]
    assert failure["provider"] == "firecrawl"
    assert failure["type"] == "TimeoutError"
    assert "firecrawl read timeout" in failure["message"]
    assert "firecrawl" in result.get("warning", "")


@pytest.mark.asyncio
async def test_web_map_falls_back_to_basic_http(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    async def fake_basic_map(url, max_depth, max_breadth, limit, timeout, ctx=None):
        assert url == "https://example.com"
        assert max_depth == 2
        return json.dumps(
            {
                "base_url": url,
                "provider": "basic-http",
                "results": [{"url": url, "depth": 0, "links": []}],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(server, "_call_basic_http_map", fake_basic_map)
    result = await server.web_map("https://example.com", max_depth=2, max_breadth=5, limit=10, timeout=20)
    payload = json.loads(result)
    assert payload["provider"] == "basic-http"
    assert payload["results"][0]["url"] == "https://example.com"


def test_server_app_is_lazily_available():
    app = server.app
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"


def test_http_app_protects_mcp_route():
    app = server.create_http_app(mcp_path="/mcp", server_api_key="secret-token")
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        unauthorized = client.get("/mcp")
        assert unauthorized.status_code == 401

        authorized = client.get("/mcp", headers={"Authorization": "Bearer secret-token"})
        assert authorized.status_code != 401
