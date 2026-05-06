import httpx

import web_search


def test_sanitize_proxy_environment_drops_invalid_all_proxy_when_http_fallback_exists():
    env = {
        "HTTP_PROXY": "http://127.0.0.1:10808/",
        "HTTPS_PROXY": "http://127.0.0.1:10808/",
        "ALL_PROXY": "socks://127.0.0.1:10808/",
        "all_proxy": "socks://127.0.0.1:10808/",
    }

    warnings = web_search.sanitize_proxy_environment(env)

    assert "ALL_PROXY" not in env
    assert "all_proxy" not in env
    assert env["HTTP_PROXY"] == "http://127.0.0.1:10808/"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:10808/"
    assert any("ALL_PROXY" in warning for warning in warnings)


def test_sanitize_proxy_environment_rejects_only_invalid_socks_proxy():
    env = {"ALL_PROXY": "socks://127.0.0.1:10808/"}

    try:
        web_search.sanitize_proxy_environment(env)
    except web_search.ProxyConfigurationError as exc:
        assert "socks://" in str(exc)
        assert "httpx[socks]" in str(exc)
    else:
        raise AssertionError("expected ProxyConfigurationError")


def test_select_default_model_prefers_available_fast_text_model():
    ids = [
        "grok-imagine-image-lite",
        "grok-4.20-0309",
        "grok-4.20-fast",
    ]

    assert web_search.select_default_model(ids) == "grok-4.20-fast"


def test_format_http_status_error_includes_upstream_body_and_model_hint():
    request = httpx.Request("POST", "https://proxy.example/v1/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "message": "Model 'grok-4.1-fast' does not exist or you do not have access to it.",
                "code": "model_not_found",
            }
        },
    )
    exc = httpx.HTTPStatusError("bad request", request=request, response=response)

    message = web_search.format_http_status_error(exc, model="grok-4.1-fast")

    assert "400" in message
    assert "model_not_found" in message
    assert "grok-4.1-fast" in message
    assert "--list-models" in message


def test_format_http_status_error_handles_unread_streaming_response():
    request = httpx.Request("POST", "https://proxy.example/v1/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        stream=httpx.ByteStream(b'{"error":{"code":"model_not_found"}}'),
    )
    exc = httpx.HTTPStatusError("bad request", request=request, response=response)

    message = web_search.format_http_status_error(exc, model="grok-4.1-fast")

    assert "400" in message
    assert "Response body was not read" in message
    assert "--list-models" in message
