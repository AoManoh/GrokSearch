from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_export_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "grok2api"
        / "scripts"
        / "export_groksearch_mcp_json.py"
    )
    spec = importlib.util.spec_from_file_location("export_groksearch_mcp_json", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_build_mcp_payload_portable_stdio(monkeypatch):
    module = _load_export_module()
    args = SimpleNamespace(
        base_url="https://grok.example.com",
        scheme=None,
        api_key="secret-key",
        client_transport="stdio",
        stdio_launcher="portable",
        fallback_api_key="dummy",
        mcp_url=None,
        mcp_server_api_key=None,
        model="grok-4.1-fast",
        groksearch_dir=None,
        package_spec="git+https://github.com/AoManoh/GrokSearch.git",
        name="grok-search",
        output=None,
    )

    payload = module.build_mcp_payload(args, {})

    assert payload["type"] == "stdio"
    assert payload["command"] == "uvx"
    assert payload["args"][:2] == ["--from", "git+https://github.com/AoManoh/GrokSearch.git"]
    assert all(not str(item).startswith("/home/") for item in payload["args"])
    assert payload["env"]["GROK_API_URL"] == "https://grok.example.com/v1"
    assert payload["env"]["GROK_API_KEY"] == "secret-key"


def test_build_mcp_payload_streamable_http():
    module = _load_export_module()
    args = SimpleNamespace(
        base_url="https://grok.example.com",
        scheme=None,
        api_key="secret-key",
        client_transport="streamable-http",
        stdio_launcher="portable",
        fallback_api_key="dummy",
        mcp_url=None,
        mcp_server_api_key="server-key",
        model=None,
        groksearch_dir=None,
        package_spec=None,
        name="grok-search",
        output=None,
    )

    payload = module.build_mcp_payload(args, {})

    assert payload["type"] == "streamable-http"
    assert payload["url"] == "https://grok.example.com/mcp"
    assert payload["headers"]["Authorization"] == "Bearer server-key"
