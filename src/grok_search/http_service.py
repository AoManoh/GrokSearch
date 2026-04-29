from __future__ import annotations

import argparse
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastmcp.server.http import Middleware
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from .config import config
from .providers.grok import GrokSearchProvider
from .server import _fetch_available_models, _pick_fallback_model, mcp

DEFAULT_MCP_SERVER_NAME = "grok-search"
DEFAULT_MCP_PATH = "/mcp"
DEFAULT_MCP_TRANSPORT = "streamable-http"
DEFAULT_MCP_HOST = "0.0.0.0"
DEFAULT_MCP_PORT = 8765
DEFAULT_MCP_HEALTH_PATH = "/health"
DEFAULT_MCP_READY_PATH = "/ready"
DEFAULT_MCP_CONFIG_PATH = "/.well-known/mcp-config"
DEFAULT_API_KEY_PLACEHOLDER = "<replace-with-your-mcp-api-key>"
DEFAULT_READY_CHECKS = ("models",)
RETRYABLE_UPSTREAM_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_HTTP_ROUTES_REGISTERED = False


def _normalize_path(value: str | None, default: str) -> str:
    path = (value or default).strip()
    if not path:
        path = default
    if not path.startswith("/"):
        path = f"/{path}"
    if len(path) > 1:
        path = path.rstrip("/")
    return path or default


def _parse_api_keys(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    parts = []
    for item in value.replace(";", ",").split(","):
        token = item.strip()
        if token:
            parts.append(token)
    return tuple(parts)


def _parse_ready_checks(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_READY_CHECKS

    checks: list[str] = []
    supported = {"models", "chat"}
    for item in value.replace(";", ",").split(","):
        token = item.strip().lower()
        if not token:
            continue
        if token not in supported:
            raise ValueError(
                "MCP_READY_CHECKS 仅支持 models 和 chat，多个值请用逗号分隔。"
            )
        if token not in checks:
            checks.append(token)
    return tuple(checks) or DEFAULT_READY_CHECKS


def _extract_status_from_message(message: str) -> int | None:
    import re

    patterns = (
        re.compile(r"\bHTTP\s+([1-5]\d{2})\b", re.IGNORECASE),
        re.compile(r"\bstatus(?:=|:)?\s*([1-5]\d{2})\b", re.IGNORECASE),
        re.compile(r"\b(?:failed|error|redirect(?:ed)?)\b[^0-9]{0,12}([1-5]\d{2})\b", re.IGNORECASE),
        re.compile(r",\s*([1-5]\d{2})(?:\D|$)"),
    )
    for pattern in patterns:
        match = pattern.search(message)
        if not match:
            continue
        status_code = int(match.group(1))
        if 300 <= status_code <= 599:
            return status_code
    return None


def _serialize_upstream_exception(exc: Exception) -> dict[str, Any]:
    message = str(exc).strip() or exc.__class__.__name__
    payload: dict[str, Any] = {
        "message": message,
        "retryable": False,
    }

    status = getattr(exc, "upstream_status", None)
    retryable = getattr(exc, "retryable", None)
    if isinstance(status, int):
        payload["upstream_status"] = status
    if isinstance(retryable, bool):
        payload["retryable"] = retryable

    try:
        import httpx
    except Exception:  # pragma: no cover
        httpx = None

    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            payload["code"] = "upstream_timeout"
            payload["retryable"] = True
            return payload
        if isinstance(exc, httpx.RequestError):
            payload["code"] = "upstream_network_error"
            payload["retryable"] = True
            return payload
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            payload["code"] = "upstream_http_error"
            payload["upstream_status"] = status_code
            payload["retryable"] = status_code in RETRYABLE_UPSTREAM_STATUS_CODES
            body = exc.response.text[:200].strip()
            payload["message"] = (
                f"Grok 上游返回 HTTP {status_code}: {body}"
                if body
                else f"Grok 上游返回 HTTP {status_code}"
            )
            return payload

    if "upstream_status" not in payload:
        parsed_status = _extract_status_from_message(message)
        if parsed_status is not None:
            payload["upstream_status"] = parsed_status
            payload["retryable"] = parsed_status in RETRYABLE_UPSTREAM_STATUS_CODES

    payload.setdefault("code", "upstream_error")
    return payload


@dataclass(frozen=True)
class HttpServiceSettings:
    host: str
    port: int
    path: str
    transport: str
    public_base_url: str | None
    api_keys: tuple[str, ...]
    health_path: str
    ready_path: str
    config_path: str
    ready_checks: tuple[str, ...]

    @classmethod
    def from_env(cls, require_api_keys: bool = True) -> "HttpServiceSettings":
        raw_keys = (
            os.getenv("MCP_SERVER_API_KEYS")
            or os.getenv("MCP_SERVER_API_KEY")
            or ""
        )
        api_keys = _parse_api_keys(raw_keys)
        if require_api_keys and not api_keys:
            raise ValueError(
                "MCP_SERVER_API_KEY 未配置；远程 MCP 服务要求显式访问密钥。"
            )

        try:
            port = int(os.getenv("MCP_SERVER_PORT", str(DEFAULT_MCP_PORT)))
        except ValueError as exc:
            raise ValueError("MCP_SERVER_PORT 必须是整数。") from exc

        transport = os.getenv("MCP_SERVER_TRANSPORT", DEFAULT_MCP_TRANSPORT).strip()
        if transport not in {"http", "streamable-http", "sse"}:
            raise ValueError(
                "MCP_SERVER_TRANSPORT 仅支持 http、streamable-http 或 sse。"
            )

        public_base_url = (os.getenv("MCP_PUBLIC_BASE_URL") or "").strip() or None
        if public_base_url:
            public_base_url = public_base_url.rstrip("/")

        return cls(
            host=(os.getenv("MCP_SERVER_HOST") or DEFAULT_MCP_HOST).strip()
            or DEFAULT_MCP_HOST,
            port=port,
            path=_normalize_path(os.getenv("MCP_SERVER_PATH"), DEFAULT_MCP_PATH),
            transport=transport,
            public_base_url=public_base_url,
            api_keys=api_keys,
            health_path=DEFAULT_MCP_HEALTH_PATH,
            ready_path=DEFAULT_MCP_READY_PATH,
            config_path=DEFAULT_MCP_CONFIG_PATH,
            ready_checks=_parse_ready_checks(os.getenv("MCP_READY_CHECKS")),
        )


def build_client_config(
    base_url: str,
    api_key: str = DEFAULT_API_KEY_PLACEHOLDER,
    *,
    path: str = DEFAULT_MCP_PATH,
    transport: str = DEFAULT_MCP_TRANSPORT,
) -> dict[str, Any]:
    normalized_base_url = base_url.rstrip("/")
    normalized_path = _normalize_path(path, DEFAULT_MCP_PATH)
    return {
        "type": transport,
        "url": f"{normalized_base_url}{normalized_path}",
        "headers": {"Authorization": f"Bearer {api_key}"},
    }


def _resolve_public_base_url(request: Request, settings: HttpServiceSettings) -> str:
    if settings.public_base_url:
        return settings.public_base_url

    forwarded_host = request.headers.get("x-forwarded-host")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def build_service_config(
    base_url: str,
    settings: HttpServiceSettings,
) -> dict[str, Any]:
    normalized_base_url = base_url.rstrip("/")
    generic = build_client_config(
        normalized_base_url,
        path=settings.path,
        transport=settings.transport,
    )
    return {
        "name": DEFAULT_MCP_SERVER_NAME,
        "transport": settings.transport,
        "base_url": normalized_base_url,
        "mcp_path": settings.path,
        "health_url": f"{normalized_base_url}{settings.health_path}",
        "ready_url": f"{normalized_base_url}{settings.ready_path}",
        "config_url": f"{normalized_base_url}{settings.config_path}",
        "ready_checks": list(settings.ready_checks),
        "auth": {
            "supported_headers": ["Authorization: Bearer <api_key>", "x-api-key: <api_key>"],
            "api_keys_configured": bool(settings.api_keys),
        },
        "generic_config": generic,
        "mcpServers": {DEFAULT_MCP_SERVER_NAME: generic},
    }


def _service_payload(request: Request, settings: HttpServiceSettings) -> dict[str, Any]:
    return build_service_config(_resolve_public_base_url(request, settings), settings)


async def _probe_upstream_chat(api_url: str, api_key: str, model: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Respond with pong."},
            {"role": "user", "content": "ping"},
        ],
        "stream": True,
        "max_tokens": 1,
    }
    provider = GrokSearchProvider(api_url, api_key, model)
    content = await provider._execute_stream_with_retry(headers, payload)
    return {
        "status": "ok",
        "model": model,
        "content_length": len(content.strip()),
    }


def _extract_request_api_key(headers: Headers) -> str | None:
    x_api_key = (headers.get("x-api-key") or "").strip()
    if x_api_key:
        return x_api_key

    authorization = (headers.get("authorization") or "").strip()
    if not authorization:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer":
        return token.strip() or None
    return authorization


class APIKeyAuthMiddleware:
    def __init__(
        self,
        app,
        *,
        api_keys: tuple[str, ...],
        exempt_paths: tuple[str, ...],
    ):
        self.app = app
        self.api_keys = api_keys
        self.exempt_paths = frozenset(exempt_paths)

    def _is_authorized(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return any(hmac.compare_digest(candidate, allowed) for allowed in self.api_keys)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "").upper()
        if path in self.exempt_paths or method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        token = _extract_request_api_key(headers)
        if self._is_authorized(token):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {
                "error": "invalid_api_key",
                "message": "缺少有效的 MCP 访问密钥，请使用 Authorization Bearer 或 x-api-key。",
            },
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="grok-search-mcp"'},
        )
        await response(scope, receive, send)


def _ensure_http_routes_registered() -> None:
    global _HTTP_ROUTES_REGISTERED
    if _HTTP_ROUTES_REGISTERED:
        return

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def service_index(request: Request) -> Response:
        settings = HttpServiceSettings.from_env(require_api_keys=False)
        return JSONResponse(_service_payload(request, settings))

    @mcp.custom_route(DEFAULT_MCP_HEALTH_PATH, methods=["GET"], include_in_schema=False)
    async def service_health(_: Request) -> Response:
        settings = HttpServiceSettings.from_env(require_api_keys=False)
        return JSONResponse(
            {
                "status": "ok",
                "service": DEFAULT_MCP_SERVER_NAME,
                "transport": settings.transport,
                "mcp_path": settings.path,
            }
        )

    @mcp.custom_route(DEFAULT_MCP_READY_PATH, methods=["GET"], include_in_schema=False)
    async def service_ready(_: Request) -> Response:
        settings = HttpServiceSettings.from_env(require_api_keys=False)
        checks: dict[str, Any] = {}

        try:
            api_url = config.grok_api_url
            api_key = config.grok_api_key
        except ValueError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "reason": "config",
                    "message": str(exc),
                    "checks": checks,
                    "ready_checks": list(settings.ready_checks),
                },
                status_code=503,
            )

        models: list[str] = []
        if "models" in settings.ready_checks:
            try:
                models = await _fetch_available_models(api_url, api_key)
                checks["models"] = {
                    "status": "ok",
                    "models_count": len(models),
                    "models": models,
                }
            except Exception as exc:  # pragma: no cover - 真实网络错误靠集成验证覆盖
                checks["models"] = {
                    "status": "error",
                    **_serialize_upstream_exception(exc),
                }
                return JSONResponse(
                    {
                        "status": "error",
                        "reason": "models",
                        "message": checks["models"]["message"],
                        "checks": checks,
                        "ready_checks": list(settings.ready_checks),
                    },
                    status_code=503,
                )

        if "chat" in settings.ready_checks:
            if models:
                probe_model = _pick_fallback_model(models)
            else:
                probe_model = config.grok_model
            try:
                checks["chat"] = await _probe_upstream_chat(api_url, api_key, probe_model)
            except Exception as exc:  # pragma: no cover - 真实网络错误靠集成验证覆盖
                checks["chat"] = {
                    "status": "error",
                    "model": probe_model,
                    **_serialize_upstream_exception(exc),
                }
                return JSONResponse(
                    {
                        "status": "error",
                        "reason": "chat",
                        "message": checks["chat"]["message"],
                        "checks": checks,
                        "ready_checks": list(settings.ready_checks),
                    },
                    status_code=503,
                )

        response_payload = {
            "status": "ok",
            "checks": checks,
            "ready_checks": list(settings.ready_checks),
        }
        if "models" in checks:
            response_payload["models_count"] = checks["models"]["models_count"]
            response_payload["models"] = checks["models"]["models"]
        return JSONResponse(response_payload)

    @mcp.custom_route(DEFAULT_MCP_CONFIG_PATH, methods=["GET"], include_in_schema=False)
    async def service_config(request: Request) -> Response:
        settings = HttpServiceSettings.from_env(require_api_keys=False)
        return JSONResponse(_service_payload(request, settings))

    _HTTP_ROUTES_REGISTERED = True


def build_http_app(
    settings: HttpServiceSettings | None = None,
):
    resolved = settings or HttpServiceSettings.from_env(require_api_keys=True)
    _ensure_http_routes_registered()

    middleware = [
        Middleware(
            APIKeyAuthMiddleware,
            api_keys=resolved.api_keys,
            exempt_paths=(
                resolved.health_path,
                resolved.ready_path,
                resolved.config_path,
                "/",
            ),
        )
    ]

    inner_app = mcp.http_app(
        path=resolved.path,
        transport=resolved.transport,
        middleware=middleware,
    )
    return Starlette(
        routes=[Mount("/", app=inner_app)],
        lifespan=inner_app.lifespan,
    )


def export_client_config() -> None:
    parser = argparse.ArgumentParser(
        description="导出远程 Grok Search MCP 的客户端 JSON 配置。"
    )
    parser.add_argument("--base-url", required=True, help="远程 MCP 服务对外基址。")
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY_PLACEHOLDER,
        help="写入 JSON 的客户端访问密钥占位值。",
    )
    parser.add_argument(
        "--format",
        choices=["generic", "mcpServers", "service"],
        default="mcpServers",
        help="输出通用配置或 mcpServers 包装格式。",
    )
    args = parser.parse_args()

    settings = HttpServiceSettings.from_env(require_api_keys=False)
    payload = build_client_config(
        args.base_url,
        api_key=args.api_key,
        path=settings.path,
        transport=settings.transport,
    )
    if args.format == "mcpServers":
        payload = {DEFAULT_MCP_SERVER_NAME: payload}
    elif args.format == "service":
        payload = build_service_config(args.base_url, settings)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    settings = HttpServiceSettings.from_env(require_api_keys=True)
    app = build_http_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=config.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


__all__ = [
    "APIKeyAuthMiddleware",
    "DEFAULT_API_KEY_PLACEHOLDER",
    "HttpServiceSettings",
    "build_client_config",
    "build_service_config",
    "build_http_app",
    "export_client_config",
    "main",
]
