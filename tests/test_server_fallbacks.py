import json
import sys
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
