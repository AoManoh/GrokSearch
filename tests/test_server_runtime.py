import httpx
import pytest

from grok_search import server
from grok_search.providers.grok import GrokSearchProvider, UpstreamSSEError


@pytest.mark.asyncio
async def test_web_search_returns_structured_error(monkeypatch):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(401, request=request, content=b"bad key")

    class RaisingProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            raise httpx.HTTPStatusError(
                "401 Unauthorized",
                request=request,
                response=response,
            )

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    monkeypatch.setenv("GROK_API_URL", "https://example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "bad-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", RaisingProvider)

    result = await server.web_search("latest grok news")

    assert result["status"] == "error"
    assert result["error"]["code"] == "upstream_http_error"
    assert result["error"]["upstream_status"] == 401
    assert "HTTP 401" in result["content"]


@pytest.mark.asyncio
async def test_web_search_respects_tavily_enabled(monkeypatch):
    called = False

    class SuccessProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            return "Answer only"

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    async def fake_tavily(query: str, max_results: int = 6) -> list[dict]:
        nonlocal called
        called = True
        return [{"url": "https://example.test", "title": "Example"}]

    monkeypatch.setenv("GROK_API_URL", "https://example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "good-key")
    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "chat")
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", SuccessProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)

    result = await server.web_search("query", extra_sources=3)

    assert result["status"] == "ok"
    assert result["sources_count"] == 0
    assert called is False


@pytest.mark.asyncio
async def test_model_cache_does_not_cache_failures(monkeypatch):
    attempts = 0

    async def fake_fetch(api_url: str, api_key: str) -> list[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return ["grok-4.1-fast"]

    async with server._AVAILABLE_MODELS_LOCK:
        server._AVAILABLE_MODELS_CACHE.clear()

    monkeypatch.setattr(server, "_fetch_available_models", fake_fetch)

    first = await server._get_available_models_cached("https://example.test/v1", "k")
    second = await server._get_available_models_cached("https://example.test/v1", "k")

    assert first == []
    assert second == ["grok-4.1-fast"]
    assert attempts == 2


@pytest.mark.asyncio
async def test_parse_streaming_response_raises_on_sse_error():
    provider = GrokSearchProvider("https://example.test/v1", "key")

    class FakeResponse:
        async def aiter_lines(self):
            for line in (
                "event: error",
                'data: {"error":{"message":"AppChatReverse: Chat failed, 403","code":"upstream_error"}}',
                "data: [DONE]",
            ):
                yield line

    with pytest.raises(UpstreamSSEError, match="Chat failed, 403") as exc_info:
        await provider._parse_streaming_response(FakeResponse())
    assert exc_info.value.upstream_status == 403


@pytest.mark.asyncio
async def test_web_search_surfaces_sse_upstream_status(monkeypatch):
    class RaisingProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search(self, query: str, platform: str = "") -> str:
            raise UpstreamSSEError(
                "AppChatReverse: Chat failed, 403",
                upstream_status=403,
            )

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    monkeypatch.setenv("GROK_API_URL", "https://example.test/v1")
    monkeypatch.setenv("GROK_API_KEY", "bad-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "GrokSearchProvider", RaisingProvider)

    result = await server.web_search("latest grok news")

    assert result["status"] == "error"
    assert result["error"]["code"] == "upstream_error"
    assert result["error"]["upstream_status"] == 403
    assert "流式错误" in result["content"]
