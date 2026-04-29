from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport

from grok_search.config import config
from grok_search.http_service import (
    DEFAULT_API_KEY_PLACEHOLDER,
    DEFAULT_MCP_SERVER_NAME,
    build_client_config,
    build_service_config,
    build_http_app,
)
from grok_search.providers.grok import GrokSearchProvider
import grok_search.http_service as http_service
import grok_search.server as server_mod


def _reset_runtime_state() -> None:
    config._cached_model = None
    server_mod._AVAILABLE_MODELS_CACHE.clear()


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_API_URL", "http://upstream.local/v1")
    monkeypatch.setenv("GROK_API_KEY", "upstream-secret")
    monkeypatch.setenv("GROK_MODEL", "grok-4.1-fast")
    monkeypatch.setenv("MCP_SERVER_API_KEY", "mcp-secret")
    monkeypatch.setenv("MCP_SERVER_TRANSPORT", "streamable-http")
    monkeypatch.delenv("MCP_PUBLIC_BASE_URL", raising=False)
    _reset_runtime_state()


def _httpx_client_factory(app) -> Callable[..., httpx.AsyncClient]:
    def factory(**kwargs) -> httpx.AsyncClient:
        headers = kwargs.pop("headers", None)
        auth = kwargs.pop("auth", None)
        timeout = kwargs.pop("timeout", None)
        follow_redirects = kwargs.pop("follow_redirects", True)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
            auth=auth,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )

    return factory


def _extract_tool_payload(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"content": text}
    raise AssertionError("未从 tool result 中提取到结构化内容")


@pytest.mark.asyncio
async def test_config_route_exports_remote_mcp_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    app = build_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
        ) as client:
            response = await client.get(
                "/.well-known/mcp-config",
                headers={
                    "x-forwarded-proto": "https",
                    "x-forwarded-host": "search.example.com",
                },
            )

    assert response.status_code == 200
    payload = response.json()
    expected = build_client_config(
        "https://search.example.com",
        path="/mcp",
        transport="streamable-http",
    )
    assert payload["generic_config"] == expected
    assert payload["mcpServers"][DEFAULT_MCP_SERVER_NAME] == expected
    assert payload["ready_checks"] == ["models"]
    assert payload["generic_config"]["headers"]["Authorization"] == (
        f"Bearer {DEFAULT_API_KEY_PLACEHOLDER}"
    )


@pytest.mark.asyncio
async def test_mcp_endpoint_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    app = build_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/mcp")

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_ready_route_checks_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)

    async def fake_fetch_available_models(api_url: str, api_key: str) -> list[str]:
        assert api_url == "http://upstream.local/v1"
        assert api_key == "upstream-secret"
        return ["grok-4.1-fast", "grok-4"]

    monkeypatch.setattr(http_service, "_fetch_available_models", fake_fetch_available_models)
    app = build_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["models_count"] == 2
    assert payload["checks"]["models"]["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_route_can_probe_chat_and_surface_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv("MCP_READY_CHECKS", "models,chat")

    async def fake_fetch_available_models(api_url: str, api_key: str) -> list[str]:
        return ["grok-4.1-fast"]

    async def fake_probe_chat(api_url: str, api_key: str, model: str) -> dict:
        assert api_url == "http://upstream.local/v1"
        assert api_key == "upstream-secret"
        assert model == "grok-4.1-fast"
        raise RuntimeError("AppChatReverse: Chat failed, 403")

    monkeypatch.setattr(http_service, "_fetch_available_models", fake_fetch_available_models)
    monkeypatch.setattr(http_service, "_probe_upstream_chat", fake_probe_chat)
    app = build_http_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["reason"] == "chat"
    assert payload["ready_checks"] == ["models", "chat"]
    assert payload["checks"]["chat"]["upstream_status"] == 403


def test_build_service_config_includes_ready_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv("MCP_READY_CHECKS", "models,chat")

    settings = http_service.HttpServiceSettings.from_env(require_api_keys=False)
    payload = build_service_config("https://search.example.com", settings)

    assert payload["ready_checks"] == ["models", "chat"]
    assert payload["ready_url"] == "https://search.example.com/ready"


def test_export_client_config_supports_service_format(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv("MCP_READY_CHECKS", "models,chat")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "grok-search-http-config",
            "--base-url",
            "https://search.example.com",
            "--format",
            "service",
        ],
    )

    http_service.export_client_config()

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready_checks"] == ["models", "chat"]
    assert payload["mcpServers"][DEFAULT_MCP_SERVER_NAME]["url"] == (
        "https://search.example.com/mcp"
    )


@pytest.mark.asyncio
async def test_remote_client_can_call_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)

    async def fake_available_models(api_url: str, api_key: str) -> list[str]:
        assert api_url == "http://upstream.local/v1"
        assert api_key == "upstream-secret"
        return ["grok-4.1-fast"]

    async def fake_search(
        self: GrokSearchProvider,
        query: str,
        platform: str = "",
        min_results: int = 3,
        max_results: int = 10,
        ctx=None,
    ) -> str:
        assert query == "最新 FastAPI 教程"
        assert platform == ""
        return "mocked answer"

    monkeypatch.setattr(server_mod, "_get_available_models_cached", fake_available_models)
    monkeypatch.setattr(GrokSearchProvider, "search", fake_search)

    app = build_http_app()
    transport = StreamableHttpTransport(
        "http://testserver/mcp",
        auth="mcp-secret",
        httpx_client_factory=_httpx_client_factory(app),
    )

    async with app.router.lifespan_context(app):
        async with Client(transport) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            assert "web_search" in tool_names
            result = await client.call_tool("web_search", {"query": "最新 FastAPI 教程"})

    payload = _extract_tool_payload(result)
    assert payload["content"] == "mocked answer"
    assert payload["sources_count"] == 0
