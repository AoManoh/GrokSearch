import pytest

from grok_search import server
from grok_search.providers.base import SearchResponse
from grok_search.providers.grok import GrokSearchProvider
from grok_search.providers.responses import ResponsesSearchProvider


def test_auto_provider_uses_responses_for_official_xai(monkeypatch):
    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "auto")

    mode = server._resolve_search_provider_mode("https://api.x.ai/v1")

    assert mode == "responses"


def test_auto_provider_uses_chat_for_openai_compatible_urls(monkeypatch):
    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "auto")

    mode = server._resolve_search_provider_mode("http://127.0.0.1:8000/v1")

    assert mode == "chat"


def test_explicit_provider_overrides_auto(monkeypatch):
    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "responses")

    mode = server._resolve_search_provider_mode("http://127.0.0.1:8000/v1")

    assert mode == "responses"


def test_build_search_provider_creates_expected_class(monkeypatch):
    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "responses")
    responses_provider, responses_mode = server._build_search_provider("https://api.x.ai/v1", "key", "grok-4.20-reasoning")
    assert isinstance(responses_provider, ResponsesSearchProvider)
    assert responses_mode == "responses"

    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "chat")
    chat_provider, chat_mode = server._build_search_provider("https://api.x.ai/v1", "key", "grok-4.1-fast")
    assert isinstance(chat_provider, GrokSearchProvider)
    assert chat_mode == "chat"


@pytest.mark.asyncio
async def test_web_search_returns_partial_when_sources_exist_without_answer(monkeypatch):
    class SourceOnlyProvider:
        def __init__(self, api_url: str, api_key: str, model: str):
            self.api_url = api_url
            self.api_key = api_key
            self.model = model

        async def search_response(self, query: str, platform: str = "") -> SearchResponse:
            return SearchResponse(
                answer="",
                sources=[{"url": "https://example.com/source-only"}],
                provider="xai-responses",
                model=self.model,
            )

    async def fake_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.20-reasoning"]

    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "key")
    monkeypatch.setenv("GROK_SEARCH_PROVIDER", "responses")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(server, "ResponsesSearchProvider", SourceOnlyProvider)

    result = await server.web_search("AIGC")

    assert result["status"] == "partial"
    assert result["content"] == ""
    assert result["sources_count"] == 1
    assert result["provider"] == "xai-responses"
