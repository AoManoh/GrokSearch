import pytest

from grok_search.providers.responses import ResponsesSearchProvider, parse_responses_api_payload


def test_parse_responses_payload_reads_top_level_citations():
    payload = {
        "id": "resp_1",
        "model": "grok-4.20-reasoning",
        "output_text": "AIGC is artificial intelligence generated content.",
        "citations": ["https://example.com/aigc", {"url": "https://example.com/b", "title": "B"}],
    }

    result = parse_responses_api_payload(payload)

    assert result.answer == "AIGC is artificial intelligence generated content."
    assert result.sources == [
        {"url": "https://example.com/aigc"},
        {"url": "https://example.com/b", "title": "B"},
    ]
    assert result.provider == "xai-responses"
    assert result.model == "grok-4.20-reasoning"


def test_parse_responses_payload_reads_output_annotations():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "论文降重需要保留原意并规范引用。",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Citation 1",
                                "url": "https://example.com/paper",
                                "start_index": 0,
                                "end_index": 4,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    result = parse_responses_api_payload(payload)

    assert result.answer == "论文降重需要保留原意并规范引用。"
    assert result.sources == [{"url": "https://example.com/paper", "title": "Citation 1"}]


def test_parse_responses_payload_keeps_sources_when_text_empty():
    payload = {
        "output_text": "",
        "citations": [{"url": "https://example.com/source-only", "title": "Source Only"}],
    }

    result = parse_responses_api_payload(payload)

    assert result.answer == ""
    assert result.sources == [{"url": "https://example.com/source-only", "title": "Source Only"}]


def test_parse_responses_payload_reads_inline_citation_shapes():
    payload = {
        "output_text": "Fact [[1]](https://example.com/inline).",
        "inline_citations": [
            {"web_citation": {"url": "https://example.com/web", "title": "Web"}},
            {"x_citation": {"url": "https://x.com/example/status/1", "title": "X"}},
        ],
    }

    result = parse_responses_api_payload(payload)

    assert result.sources == [
        {"url": "https://example.com/web"},
        {"url": "https://x.com/example/status/1"},
        {"url": "https://example.com/inline"},
    ]


@pytest.mark.asyncio
async def test_responses_provider_posts_to_responses_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": "ok", "citations": ["https://example.com"]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("grok_search.providers.responses.httpx.AsyncClient", FakeClient)
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "0")
    provider = ResponsesSearchProvider("https://api.x.ai/v1", "key", "grok-4.20-reasoning")

    result = await provider.search_response("latest xAI news")

    assert captured["url"] == "https://api.x.ai/v1/responses"
    assert captured["payload"]["tools"] == [{"type": "web_search"}]
    assert captured["payload"]["model"] == "grok-4.20-reasoning"
    assert result.answer == "ok"
    assert result.sources == [{"url": "https://example.com"}]
