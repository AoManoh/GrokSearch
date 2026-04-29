import argparse
import asyncio
import time
import json
import re
import sys
from collections import deque
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

# 支持直接运行：添加 src 目录到 Python 路径
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from fastmcp import FastMCP, Context
from typing import Annotated, Optional
from pydantic import Field

# 尝试使用绝对导入（支持 mcp run）
try:
    from grok_search.providers.base import SearchResponse
    from grok_search.providers.grok import GrokSearchProvider, UpstreamSSEError
    from grok_search.providers.responses import ResponsesSearchProvider
    from grok_search.logger import log_info
    from grok_search.config import config
    from grok_search.sources import SourcesCache, merge_sources, new_session_id, split_answer_and_sources
    from grok_search.planning import engine as planning_engine, _split_csv
except ImportError:
    from .providers.base import SearchResponse
    from .providers.grok import GrokSearchProvider, UpstreamSSEError
    from .providers.responses import ResponsesSearchProvider
    from .logger import log_info
    from .config import config
    from .sources import SourcesCache, merge_sources, new_session_id, split_answer_and_sources
    from .planning import engine as planning_engine, _split_csv

mcp = FastMCP("grok-search")

_SOURCES_CACHE = SourcesCache(max_size=256)
_AVAILABLE_MODELS_CACHE: dict[tuple[str, str], tuple[list[str], float]] = {}
_AVAILABLE_MODELS_LOCK = asyncio.Lock()
_AVAILABLE_MODELS_CACHE_TTL_SECONDS = 300.0


async def _fetch_available_models(api_url: str, api_key: str) -> list[str]:
    import httpx

    models_url = f"{api_url.rstrip('/')}/models"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            models_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

    models: list[str] = []
    for item in (data or {}).get("data", []) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    return models


async def _get_available_models_cached(api_url: str, api_key: str) -> list[str]:
    key = (api_url, api_key)
    now = time.monotonic()
    async with _AVAILABLE_MODELS_LOCK:
        cached = _AVAILABLE_MODELS_CACHE.get(key)
        if cached:
            models, expires_at = cached
            if expires_at > now:
                return models
            _AVAILABLE_MODELS_CACHE.pop(key, None)

    try:
        models = await _fetch_available_models(api_url, api_key)
    except Exception:
        models = []
        return models

    async with _AVAILABLE_MODELS_LOCK:
        _AVAILABLE_MODELS_CACHE[key] = (
            models,
            time.monotonic() + _AVAILABLE_MODELS_CACHE_TTL_SECONDS,
        )
    return models


def _pick_fallback_model(available: list[str]) -> str:
    preferred = [
        config._apply_model_suffix(config._DEFAULT_MODEL),
        config._DEFAULT_MODEL,
        "grok-4.1-fast",
        "grok-4",
        "grok-4.1-mini",
        "grok-3",
    ]
    for candidate in preferred:
        if candidate in available:
            return candidate
    return available[0]


def _resolve_search_provider_mode(api_url: str) -> str:
    mode = config.grok_search_provider
    if mode != "auto":
        return mode
    hostname = (urlparse(api_url).hostname or "").lower()
    if hostname == "api.x.ai" or hostname.endswith(".api.x.ai"):
        return "responses"
    return "chat"


def _build_search_provider(api_url: str, api_key: str, model: str):
    provider_mode = _resolve_search_provider_mode(api_url)
    if provider_mode == "responses":
        return ResponsesSearchProvider(api_url, api_key, model), provider_mode
    return GrokSearchProvider(api_url, api_key, model), provider_mode


def _is_tavily_available() -> bool:
    return config.tavily_enabled and bool(config.tavily_api_key)


def _extract_upstream_status(exc: Exception) -> int | None:
    status = getattr(exc, "upstream_status", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _build_upstream_error(session_id: str, exc: Exception) -> dict:
    message = str(exc).strip() or exc.__class__.__name__
    error = {
        "code": "upstream_error",
        "message": f"Grok 上游请求失败: {message}",
        "provider": "grok",
        "retryable": False,
    }

    try:
        import httpx
    except Exception:  # pragma: no cover
        httpx = None

    if isinstance(exc, UpstreamSSEError):
        error["message"] = f"Grok 上游流式错误: {message}"
        if exc.upstream_status is not None:
            error["retryable"] = exc.retryable
            error["upstream_status"] = exc.upstream_status

    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            error["code"] = "upstream_timeout"
            error["retryable"] = True
        elif isinstance(exc, httpx.RequestError):
            error["code"] = "upstream_network_error"
            error["retryable"] = True
        elif isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            error["code"] = "upstream_http_error"
            error["retryable"] = status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            error["upstream_status"] = status_code
            body = exc.response.text[:200].strip()
            if body:
                error["message"] = f"Grok 上游返回 HTTP {status_code}: {body}"
            else:
                error["message"] = f"Grok 上游返回 HTTP {status_code}"

    status_code = _extract_upstream_status(exc)
    if status_code is not None and "upstream_status" not in error:
        error["upstream_status"] = status_code

    return {
        "session_id": session_id,
        "status": "error",
        "content": error["message"],
        "sources_count": 0,
        "error": error,
    }


def _build_client_error(
    session_id: str,
    code: str,
    message: str,
    *,
    provider: str = "server",
) -> dict:
    return {
        "session_id": session_id,
        "status": "error",
        "content": message,
        "sources_count": 0,
        "error": {
            "code": code,
            "message": message,
            "provider": provider,
            "retryable": False,
        },
    }


def _build_grok_provider(model: str = "") -> GrokSearchProvider | None:
    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
    except ValueError:
        return None
    return GrokSearchProvider(api_url, api_key, model or config.grok_model)


class _HtmlLinkParser(HTMLParser):
    """提取页面标题和链接的极简 HTML 解析器。"""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.links: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() == "title":
            self._in_title = True
            return
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)
                break

    def handle_endtag(self, tag: str):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str):
        if self._in_title and data:
            self.title += data


def _strip_html_to_text(html: str) -> str:
    """把 HTML 压成基础纯文本，作为最终降级输出。"""
    content = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", html)
    content = re.sub(r"(?i)</(p|div|section|article|li|h[1-6]|tr)>", "\n", content)
    content = re.sub(r"(?s)<[^>]+>", " ", content)
    content = unescape(content)
    content = re.sub(r"[ \t]+", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def _normalize_target_url(raw_url: str, base_url: str) -> str | None:
    candidate = raw_url.strip()
    if not candidate:
        return None
    normalized = urldefrag(urljoin(base_url, candidate))[0]
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return normalized


async def _call_basic_http_fetch(url: str) -> str | None:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.ConnectError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc) and "certificate verify failed" not in str(exc).lower():
            return None
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception:
            return None
    except Exception:
        return None

    text = response.text or ""
    if not text.strip():
        return None

    parser = _HtmlLinkParser()
    try:
        parser.feed(text)
    except Exception:
        parser = _HtmlLinkParser()

    title = parser.title.strip() or url
    body = _strip_html_to_text(text)
    if not body:
        return None
    preview = body[:12000]
    return f"# {title}\n\n来源：{response.url}\n\n{preview}"


async def _call_basic_http_map(
    url: str,
    max_depth: int,
    max_breadth: int,
    limit: int,
    timeout: int,
    ctx: Context = None,
) -> str:
    import httpx

    start = time.perf_counter()
    base = _normalize_target_url(url, url)
    if not base:
        return json.dumps({"error": "invalid_url", "base_url": url}, ensure_ascii=False, indent=2)

    parsed_base = urlparse(base)
    queue = deque([(base, 0)])
    queued = {base}
    visited: set[str] = set()
    results: list[dict] = []

    try:
        async def fetch_response(target: str):
            try:
                async with httpx.AsyncClient(timeout=float(timeout), follow_redirects=True) as client:
                    response = await client.get(target)
                    response.raise_for_status()
                    return response
            except httpx.ConnectError as exc:
                if "CERTIFICATE_VERIFY_FAILED" not in str(exc) and "certificate verify failed" not in str(exc).lower():
                    raise
                async with httpx.AsyncClient(timeout=float(timeout), follow_redirects=True, verify=False) as client:
                    response = await client.get(target)
                    response.raise_for_status()
                    return response

        while queue and len(results) < limit:
            current, depth = queue.popleft()
            if current in visited:
                continue
            visited.add(current)

            try:
                response = await fetch_response(current)
            except Exception as exc:
                await log_info(ctx, f"basic map skip {current}: {exc}", config.debug_enabled)
                continue

            parser = _HtmlLinkParser()
            try:
                parser.feed(response.text or "")
            except Exception:
                pass

            links: list[str] = []
            for candidate in parser.links:
                normalized = _normalize_target_url(candidate, str(response.url))
                if not normalized:
                    continue
                parsed = urlparse(normalized)
                if parsed.netloc != parsed_base.netloc:
                    continue
                if normalized not in links:
                    links.append(normalized)
                if depth + 1 < max_depth and normalized not in queued and len(queued) < limit * 4:
                    queued.add(normalized)
                    queue.append((normalized, depth + 1))
                if len(links) >= max_breadth:
                    break

            results.append(
                {
                    "url": str(response.url),
                    "title": parser.title.strip() or str(response.url),
                    "depth": depth,
                    "links": links,
                    "provider": "basic-http",
                }
            )
    except Exception as exc:
        return json.dumps({"error": str(exc), "base_url": base}, ensure_ascii=False, indent=2)

    return json.dumps(
        {
            "base_url": base,
            "results": results,
            "response_time": round(time.perf_counter() - start, 3),
            "provider": "basic-http",
        },
        ensure_ascii=False,
        indent=2,
    )


def _extra_results_to_sources(
    tavily_results: list[dict] | None,
    firecrawl_results: list[dict] | None,
) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()

    if firecrawl_results:
        for r in firecrawl_results:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            item: dict = {"url": url, "provider": "firecrawl"}
            title = (r.get("title") or "").strip()
            if title:
                item["title"] = title
            desc = (r.get("description") or "").strip()
            if desc:
                item["description"] = desc
            sources.append(item)

    if tavily_results:
        for r in tavily_results:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            item: dict = {"url": url, "provider": "tavily"}
            title = (r.get("title") or "").strip()
            if title:
                item["title"] = title
            content = (r.get("content") or "").strip()
            if content:
                item["description"] = content
            sources.append(item)

    return sources


@mcp.tool(
    name="web_search",
    output_schema=None,
    description="""
    Before using this tool, please use the plan_intent tool to plan the search carefully.
    Performs a deep web search based on the given query and returns Grok's answer directly.

    This tool extracts sources if provided by upstream, caches them, and returns:
    - session_id: string (When you feel confused or curious about the main content, use this field to invoke the get_sources tool to obtain the corresponding list of information sources)
    - content: string (answer only)
    - sources_count: int
    """,
    meta={"version": "2.0.0", "author": "guda.studio"},
)
async def web_search(
    query: Annotated[str, "Clear, self-contained natural-language search query."],
    platform: Annotated[str, "Target platform to focus on (e.g., 'Twitter', 'GitHub', 'Reddit'). Leave empty for general web search."] = "",
    model: Annotated[str, "Optional model ID for this request only. This value is used ONLY when user explicitly provided."] = "",
    extra_sources: Annotated[int, "Number of additional reference results from Tavily/Firecrawl. Set 0 to disable. Default 0."] = 0,
) -> dict:
    session_id = new_session_id()
    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
    except ValueError as e:
        await _SOURCES_CACHE.set(session_id, [])
        return _build_client_error(
            session_id,
            "config_error",
            f"配置错误: {str(e)}",
            provider="config",
        )

    provider_mode = _resolve_search_provider_mode(api_url)
    effective_model = config.grok_responses_model if provider_mode == "responses" else config.grok_model
    available = await _get_available_models_cached(api_url, api_key)
    if model:
        if available and model not in available:
            await _SOURCES_CACHE.set(session_id, [])
            return _build_client_error(
                session_id,
                "invalid_model",
                f"无效模型: {model}",
            )
        effective_model = model
    elif available and effective_model not in available:
        effective_model = _pick_fallback_model(available)

    grok_provider, provider_mode = _build_search_provider(api_url, api_key, effective_model)

    # 计算额外信源配额
    has_tavily = _is_tavily_available()
    has_firecrawl = bool(config.firecrawl_api_key)
    firecrawl_count = 0
    tavily_count = 0
    if extra_sources > 0:
        if has_firecrawl and has_tavily:
            firecrawl_count = round(extra_sources * 1)
            tavily_count = extra_sources - firecrawl_count
        elif has_firecrawl:
            firecrawl_count = extra_sources
        elif has_tavily:
            tavily_count = extra_sources

    # 并行执行搜索任务
    async def _safe_grok() -> tuple[SearchResponse, dict | None]:
        try:
            search_response = getattr(grok_provider, "search_response", None)
            if callable(search_response):
                return await search_response(query, platform), None
            raw_content = await grok_provider.search(query, platform)
            answer, sources = split_answer_and_sources(raw_content or "")
            return SearchResponse(
                answer=answer,
                sources=sources,
                raw_content=raw_content or "",
                provider=getattr(grok_provider, "get_provider_name", lambda: provider_mode)(),
                model=effective_model,
            ), None
        except Exception as exc:
            return SearchResponse(provider=provider_mode, model=effective_model), _build_upstream_error(session_id, exc)

    async def _safe_tavily() -> list[dict] | None:
        try:
            if tavily_count:
                return await _call_tavily_search(query, tavily_count)
        except Exception:
            return None

    async def _safe_firecrawl() -> list[dict] | None:
        try:
            if firecrawl_count:
                return await _call_firecrawl_search(query, firecrawl_count)
        except Exception:
            return None

    coros: list = [_safe_grok()]
    if tavily_count > 0:
        coros.append(_safe_tavily())
    if firecrawl_count > 0:
        coros.append(_safe_firecrawl())

    gathered = await asyncio.gather(*coros)

    grok_result, grok_error = gathered[0]
    tavily_results: list[dict] | None = None
    firecrawl_results: list[dict] | None = None
    idx = 1
    if tavily_count > 0:
        tavily_results = gathered[idx]
        idx += 1
    if firecrawl_count > 0:
        firecrawl_results = gathered[idx]

    answer = grok_result.answer
    grok_sources = grok_result.sources
    extra = _extra_results_to_sources(tavily_results, firecrawl_results)
    all_sources = merge_sources(grok_sources, extra)

    await _SOURCES_CACHE.set(session_id, all_sources)
    if grok_error and not answer:
        grok_error["sources_count"] = len(all_sources)
        if all_sources:
            grok_error["warning"] = "Grok 主搜索失败，仅缓存了额外信源，未生成主回答。"
        return grok_error

    status = "ok"
    warning = None
    if not answer:
        if all_sources:
            status = "partial"
            warning = "Grok 上游返回了信源但没有正文。"
        else:
            status = "empty"

    result = {
        "session_id": session_id,
        "status": status,
        "content": answer,
        "sources_count": len(all_sources),
        "model": effective_model,
        "provider": grok_result.provider or provider_mode,
    }
    if warning:
        result["warning"] = warning
    return result


@mcp.tool(
    name="get_sources",
    description="""
    When you feel confused or curious about the search response content, use the session_id returned by web_search to invoke the this tool to obtain the corresponding list of information sources.
    Retrieve all cached sources for a previous web_search call.
    Provide the session_id returned by web_search to get the full source list.
    """,
    meta={"version": "1.0.0", "author": "guda.studio"},
)
async def get_sources(
    session_id: Annotated[str, "Session ID from previous web_search call."]
) -> dict:
    sources = await _SOURCES_CACHE.get(session_id)
    if sources is None:
        return {
            "session_id": session_id,
            "sources": [],
            "sources_count": 0,
            "error": "session_id_not_found_or_expired",
        }
    return {"session_id": session_id, "sources": sources, "sources_count": len(sources)}


async def _call_tavily_extract(url: str) -> str | None:
    import httpx
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not api_key:
        return None
    endpoint = f"{api_url.rstrip('/')}/extract"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"urls": [url], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            if data.get("results") and len(data["results"]) > 0:
                content = data["results"][0].get("raw_content", "")
                return content if content and content.strip() else None
            return None
    except Exception:
        return None


async def _call_tavily_search(query: str, max_results: int = 6) -> list[dict] | None:
    import httpx
    api_key = config.tavily_api_key
    if not api_key:
        return None
    endpoint = f"{config.tavily_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_raw_content": False,
        "include_answer": False,
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", ""), "score": r.get("score", 0)}
                for r in results
            ] if results else None
    except Exception:
        return None


async def _call_firecrawl_search(query: str, limit: int = 14) -> list[dict] | None:
    import httpx
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"query": query, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            results = data.get("data", {}).get("web", [])
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
                for r in results
            ] if results else None
    except Exception:
        return None


async def _call_firecrawl_scrape(url: str, ctx=None) -> str | None:
    import httpx
    api_url = config.firecrawl_api_url
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    max_retries = config.retry_max_attempts
    for attempt in range(max_retries):
        body = {
            "url": url,
            "formats": ["markdown"],
            "timeout": 60000,
            "waitFor": (attempt + 1) * 1500,
        }
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
                markdown = data.get("data", {}).get("markdown", "")
                if markdown and markdown.strip():
                    return markdown
                await log_info(ctx, f"Firecrawl: markdown为空, 重试 {attempt + 1}/{max_retries}", config.debug_enabled)
        except Exception as e:
            await log_info(ctx, f"Firecrawl error: {e}", config.debug_enabled)
            return None
    return None


@mcp.tool(
    name="web_fetch",
    output_schema=None,
    description="""
    Fetches and extracts complete content from a URL, returning it as a structured Markdown document.

    **Key Features:**
        - **Full Content Extraction:** Retrieves and parses all meaningful content (text, images, links, tables, code blocks).
        - **Markdown Conversion:** Converts HTML structure to well-formatted Markdown with preserved hierarchy.
        - **Content Fidelity:** Maintains 100% content fidelity without summarization or modification.

    **Edge Cases & Best Practices:**
        - Ensure URL is complete and accessible (not behind authentication or paywalls).
        - May not capture dynamically loaded content requiring JavaScript execution.
        - Large pages may take longer to process; consider timeout implications.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def web_fetch(
    url: Annotated[str, "Valid HTTP/HTTPS web address pointing to the target page. Must be complete and accessible."],
    ctx: Context = None
) -> str:
    await log_info(ctx, f"Begin Fetch: {url}", config.debug_enabled)

    if config.tavily_enabled:
        result = await _call_tavily_extract(url)
        if result:
            await log_info(ctx, "Fetch Finished (Tavily)!", config.debug_enabled)
            return result

    await log_info(ctx, "Tavily unavailable or failed, trying Firecrawl...", config.debug_enabled)
    result = await _call_firecrawl_scrape(url, ctx)
    if result:
        await log_info(ctx, "Fetch Finished (Firecrawl)!", config.debug_enabled)
        return result

    await log_info(ctx, "Extractor unavailable, trying Grok fetch...", config.debug_enabled)
    grok_provider = _build_grok_provider()
    if grok_provider:
        try:
            result = await grok_provider.fetch(url, ctx)
            if result and result.strip():
                await log_info(ctx, "Fetch Finished (Grok)!", config.debug_enabled)
                return result
        except Exception as exc:
            await log_info(ctx, f"Grok fetch failed: {exc}", config.debug_enabled)

    await log_info(ctx, "Grok fetch unavailable, trying raw HTTP fallback...", config.debug_enabled)
    result = await _call_basic_http_fetch(url)
    if result:
        await log_info(ctx, "Fetch Finished (basic HTTP)!", config.debug_enabled)
        return result

    await log_info(ctx, "Fetch Failed!", config.debug_enabled)
    if not config.tavily_api_key and not config.firecrawl_api_key:
        try:
            config.grok_api_url
            config.grok_api_key
            return "提取失败: Grok 与基础 HTTP 回退均未能获取内容"
        except ValueError:
            return "配置错误: GROK_API_URL / GROK_API_KEY 未配置，且 TAVILY_API_KEY / FIRECRAWL_API_KEY 均未配置"
    return "提取失败: 所有提取服务均未能获取内容"


async def _call_tavily_map(url: str, instructions: str = None, max_depth: int = 1,
                           max_breadth: int = 20, limit: int = 50, timeout: int = 150) -> str:
    import httpx
    import json
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not api_key:
        return "配置错误: TAVILY_API_KEY 未配置，请设置环境变量 TAVILY_API_KEY"
    endpoint = f"{api_url.rstrip('/')}/map"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth, "limit": limit, "timeout": timeout}
    if instructions:
        body["instructions"] = instructions
    try:
        async with httpx.AsyncClient(timeout=float(timeout + 10)) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return json.dumps({
                "base_url": data.get("base_url", ""),
                "results": data.get("results", []),
                "response_time": data.get("response_time", 0)
            }, ensure_ascii=False, indent=2)
    except httpx.TimeoutException:
        return f"映射超时: 请求超过{timeout}秒"
    except httpx.HTTPStatusError as e:
        return f"HTTP错误: {e.response.status_code} - {e.response.text[:200]}"
    except Exception as e:
        return f"映射错误: {str(e)}"


@mcp.tool(
    name="web_map",
    description="""
    Maps a website's structure by traversing it like a graph, discovering URLs and generating a comprehensive site map.

    **Key Features:**
        - **Graph Traversal:** Explores website structure starting from root URL.
        - **Depth & Breadth Control:** Configure traversal limits to balance coverage and performance.
        - **Instruction Filtering:** Use natural language to focus crawler on specific content types.

    **Edge Cases & Best Practices:**
        - Start with low max_depth (1-2) for initial exploration, increase if needed.
        - Use instructions to filter for specific content (e.g., "only documentation pages").
        - Large sites may hit timeout limits; adjust timeout and limit parameters accordingly.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def web_map(
    url: Annotated[str, "Root URL to begin the mapping (e.g., 'https://docs.example.com')."],
    instructions: Annotated[str, "Natural language instructions for the crawler to filter or focus on specific content."] = "",
    max_depth: Annotated[int, Field(description="Maximum depth of mapping from the base URL.", ge=1, le=5)] = 1,
    max_breadth: Annotated[int, Field(description="Maximum number of links to follow per page.", ge=1, le=500)] = 20,
    limit: Annotated[int, Field(description="Total number of links to process before stopping.", ge=1, le=500)] = 50,
    timeout: Annotated[int, Field(description="Maximum time in seconds for the operation.", ge=10, le=150)] = 150
) -> str:
    if _is_tavily_available():
        result = await _call_tavily_map(url, instructions, max_depth, max_breadth, limit, timeout)
        if not result.startswith("配置错误:"):
            return result
    return await _call_basic_http_map(url, max_depth, max_breadth, limit, timeout)


@mcp.tool(
    name="get_config_info",
    output_schema=None,
    description="""
    Returns current Grok Search MCP server configuration and tests API connectivity.

    **Key Features:**
        - **Configuration Check:** Verifies environment variables and current settings.
        - **Connection Test:** Sends request to /models endpoint to validate API access.
        - **Model Discovery:** Lists all available models from the API.

    **Edge Cases & Best Practices:**
        - Use this tool first when debugging connection or configuration issues.
        - API keys are automatically masked for security in the response.
        - Connection test timeout is 10 seconds; network issues may cause delays.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def get_config_info() -> str:
    import json
    import httpx

    config_info = config.get_config_info()

    # 添加连接测试
    test_result = {
        "status": "未测试",
        "message": "",
        "response_time_ms": 0
    }

    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key

        # 构建 /models 端点 URL
        models_url = f"{api_url.rstrip('/')}/models"

        # 发送测试请求
        import time
        start_time = time.time()

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                models_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
            )

            response_time = (time.time() - start_time) * 1000  # 转换为毫秒

            if response.status_code == 200:
                test_result["status"] = "✅ 连接成功"
                test_result["message"] = f"成功获取模型列表 (HTTP {response.status_code})"
                test_result["response_time_ms"] = round(response_time, 2)

                # 尝试解析返回的模型列表
                try:
                    models_data = response.json()
                    if "data" in models_data and isinstance(models_data["data"], list):
                        model_count = len(models_data["data"])
                        test_result["message"] += f"，共 {model_count} 个模型"

                        # 提取所有模型的 ID/名称
                        model_names = []
                        for model in models_data["data"]:
                            if isinstance(model, dict) and "id" in model:
                                model_names.append(model["id"])

                        if model_names:
                            test_result["available_models"] = model_names
                except:
                    pass
            else:
                test_result["status"] = "⚠️ 连接异常"
                test_result["message"] = f"HTTP {response.status_code}: {response.text[:100]}"
                test_result["response_time_ms"] = round(response_time, 2)

    except httpx.TimeoutException:
        test_result["status"] = "❌ 连接超时"
        test_result["message"] = "请求超时（10秒），请检查网络连接或 API URL"
    except httpx.RequestError as e:
        test_result["status"] = "❌ 连接失败"
        test_result["message"] = f"网络错误: {str(e)}"
    except ValueError as e:
        test_result["status"] = "❌ 配置错误"
        test_result["message"] = str(e)
    except Exception as e:
        test_result["status"] = "❌ 测试失败"
        test_result["message"] = f"未知错误: {str(e)}"

    config_info["connection_test"] = test_result

    return json.dumps(config_info, ensure_ascii=False, indent=2)


@mcp.tool(
    name="switch_model",
    output_schema=None,
    description="""
    Switches the default Grok model used for search and fetch operations, persisting the setting.

    **Key Features:**
        - **Model Selection:** Change the AI model for web search and content fetching.
        - **Persistent Storage:** Model preference saved to ~/.config/grok-search/config.json.
        - **Immediate Effect:** New model used for all subsequent operations.

    **Edge Cases & Best Practices:**
        - Use get_config_info to verify available models before switching.
        - Invalid model IDs may cause API errors in subsequent requests.
        - Model changes persist across sessions until explicitly changed again.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def switch_model(
    model: Annotated[str, "Model ID to switch to (e.g., 'grok-4.1-fast', 'grok-4', 'grok-vision-beta')."]
) -> str:
    import json

    try:
        previous_model = config.grok_model
        config.set_model(model)
        current_model = config.grok_model

        result = {
            "status": "✅ 成功",
            "previous_model": previous_model,
            "current_model": current_model,
            "message": f"模型已从 {previous_model} 切换到 {current_model}",
            "config_file": str(config.config_file)
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    except ValueError as e:
        result = {
            "status": "❌ 失败",
            "message": f"切换模型失败: {str(e)}"
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        result = {
            "status": "❌ 失败",
            "message": f"未知错误: {str(e)}"
        }
        return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="toggle_builtin_tools",
    output_schema=None,
    description="""
    Toggle Claude Code's built-in WebSearch and WebFetch tools on/off.

    **Key Features:**
        - **Tool Control:** Enable or disable Claude Code's native web tools.
        - **Project Scope:** Changes apply to current project's .claude/settings.json.
        - **Status Check:** Query current state without making changes.

    **Edge Cases & Best Practices:**
        - Use "on" to block built-in tools when preferring this MCP server's implementation.
        - Use "off" to restore Claude Code's native tools.
        - Use "status" to check current configuration without modification.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def toggle_builtin_tools(
    action: Annotated[str, "Action to perform: 'on' (block built-in), 'off' (allow built-in), or 'status' (check current state)."] = "status"
) -> str:
    import json

    # Locate project root
    root = Path.cwd()
    while root != root.parent and not (root / ".git").exists():
        root = root.parent

    settings_path = root / ".claude" / "settings.json"
    tools = ["WebFetch", "WebSearch"]

    # Load or initialize
    if settings_path.exists():
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
    else:
        settings = {"permissions": {"deny": []}}

    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    blocked = all(t in deny for t in tools)

    # Execute action
    if action in ["on", "enable"]:
        for t in tools:
            if t not in deny:
                deny.append(t)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        msg = "官方工具已禁用"
        blocked = True
    elif action in ["off", "disable"]:
        deny[:] = [t for t in deny if t not in tools]
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        msg = "官方工具已启用"
        blocked = False
    else:
        msg = f"官方工具当前{'已禁用' if blocked else '已启用'}"

    return json.dumps({
        "blocked": blocked,
        "deny_list": deny,
        "file": str(settings_path),
        "message": msg
    }, ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_intent",
    output_schema=None,
    description="""
    Phase 1 of search planning: Analyze user intent. Call this FIRST to create a session.
    Returns session_id for subsequent phases. Required flow:
    plan_intent → plan_complexity → plan_sub_query(×N) → plan_search_term(×N) → plan_tool_mapping(×N) → plan_execution

    Required phases depend on complexity: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.
    """,
)
async def plan_intent(
    thought: Annotated[str, "Reasoning for this phase"],
    core_question: Annotated[str, "Distilled core question in one sentence"],
    query_type: Annotated[str, "factual | comparative | exploratory | analytical"],
    time_sensitivity: Annotated[str, "realtime | recent | historical | irrelevant"],
    session_id: Annotated[str, "Empty for new session, or existing ID to revise"] = "",
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    domain: Annotated[str, "Specific domain if identifiable"] = "",
    premise_valid: Annotated[Optional[bool], "False if the question contains a flawed assumption"] = None,
    ambiguities: Annotated[str, "Comma-separated unresolved ambiguities"] = "",
    unverified_terms: Annotated[str, "Comma-separated external terms to verify"] = "",
    is_revision: Annotated[bool, "True to overwrite existing intent"] = False,
) -> str:
    import json
    data = {"core_question": core_question, "query_type": query_type, "time_sensitivity": time_sensitivity}
    if domain:
        data["domain"] = domain
    if premise_valid is not None:
        data["premise_valid"] = premise_valid
    if ambiguities:
        data["ambiguities"] = _split_csv(ambiguities)
    if unverified_terms:
        data["unverified_terms"] = _split_csv(unverified_terms)
    return json.dumps(planning_engine.process_phase(
        phase="intent_analysis", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=data,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_complexity",
    output_schema=None,
    description="Phase 2: Assess search complexity (1-3). Controls required phases: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.",
)
async def plan_complexity(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for complexity assessment"],
    level: Annotated[int, "Complexity 1-3"],
    estimated_sub_queries: Annotated[int, "Expected number of sub-queries"],
    estimated_tool_calls: Annotated[int, "Expected total tool calls"],
    justification: Annotated[str, "Why this complexity level"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to overwrite"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    return json.dumps(planning_engine.process_phase(
        phase="complexity_assessment", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence,
        phase_data={"level": level, "estimated_sub_queries": estimated_sub_queries,
                     "estimated_tool_calls": estimated_tool_calls, "justification": justification},
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_sub_query",
    output_schema=None,
    description="Phase 3: Add one sub-query. Call once per sub-query; data accumulates across calls. Set is_revision=true to replace all.",
)
async def plan_sub_query(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this sub-query"],
    id: Annotated[str, "Unique ID (e.g., 'sq1')"],
    goal: Annotated[str, "Sub-query goal"],
    expected_output: Annotated[str, "What success looks like"],
    boundary: Annotated[str, "What this excludes — mutual exclusion with siblings"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    depends_on: Annotated[str, "Comma-separated prerequisite IDs"] = "",
    tool_hint: Annotated[str, "web_search | web_fetch | web_map"] = "",
    is_revision: Annotated[bool, "True to replace all sub-queries"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    item = {"id": id, "goal": goal, "expected_output": expected_output, "boundary": boundary}
    if depends_on:
        item["depends_on"] = _split_csv(depends_on)
    if tool_hint:
        item["tool_hint"] = tool_hint
    return json.dumps(planning_engine.process_phase(
        phase="query_decomposition", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=item,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_search_term",
    output_schema=None,
    description="Phase 4: Add one search term. Call once per term; data accumulates. First call must set approach.",
)
async def plan_search_term(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this search term"],
    term: Annotated[str, "Search query (max 8 words)"],
    purpose: Annotated[str, "Sub-query ID this serves (e.g., 'sq1')"],
    round: Annotated[int, "Execution round: 1=broad, 2+=targeted follow-up"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    approach: Annotated[str, "broad_first | narrow_first | targeted (required on first call)"] = "",
    fallback_plan: Annotated[str, "Fallback if primary searches fail"] = "",
    is_revision: Annotated[bool, "True to replace all search terms"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    data = {"search_terms": [{"term": term, "purpose": purpose, "round": round}]}
    if approach:
        data["approach"] = approach
    if fallback_plan:
        data["fallback_plan"] = fallback_plan
    return json.dumps(planning_engine.process_phase(
        phase="search_strategy", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=data,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_tool_mapping",
    output_schema=None,
    description="Phase 5: Map a sub-query to a tool. Call once per mapping; data accumulates.",
)
async def plan_tool_mapping(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this mapping"],
    sub_query_id: Annotated[str, "Sub-query ID to map"],
    tool: Annotated[str, "web_search | web_fetch | web_map"],
    reason: Annotated[str, "Why this tool for this sub-query"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    params_json: Annotated[str, "Optional JSON string for tool-specific params"] = "",
    is_revision: Annotated[bool, "True to replace all mappings"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    item = {"sub_query_id": sub_query_id, "tool": tool, "reason": reason}
    if params_json:
        try:
            item["params"] = json.loads(params_json)
        except json.JSONDecodeError:
            pass
    return json.dumps(planning_engine.process_phase(
        phase="tool_selection", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=item,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_execution",
    output_schema=None,
    description="Phase 6: Define execution order. parallel_groups: semicolon-separated groups of comma-separated IDs (e.g., 'sq1,sq2;sq3').",
)
async def plan_execution(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for execution order"],
    parallel_groups: Annotated[str, "Parallel batches: 'sq1,sq2;sq3,sq4' (semicolon=groups, comma=IDs)"],
    sequential: Annotated[str, "Comma-separated IDs that must run in order"],
    estimated_rounds: Annotated[int, "Estimated execution rounds"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to overwrite"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    parallel = [_split_csv(g) for g in parallel_groups.split(";") if g.strip()] if parallel_groups else []
    seq = _split_csv(sequential)
    return json.dumps(planning_engine.process_phase(
        phase="execution_order", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence,
        phase_data={"parallel": parallel, "sequential": seq, "estimated_rounds": estimated_rounds},
    ), ensure_ascii=False, indent=2)


def create_http_app(
    mcp_path: str | None = None,
    server_api_key: str | None = None,
):
    from grok_search.http_service import (
        DEFAULT_MCP_CONFIG_PATH,
        DEFAULT_MCP_HEALTH_PATH,
        DEFAULT_MCP_READY_PATH,
        HttpServiceSettings,
        build_http_app,
    )

    api_key = server_api_key if server_api_key is not None else config.mcp_server_api_key
    return build_http_app(
        HttpServiceSettings(
            host=config.mcp_http_host,
            port=config.mcp_http_port,
            path=config._normalize_mcp_path(mcp_path or config.mcp_http_path),
            transport="streamable-http",
            public_base_url=None,
            api_keys=(api_key,) if api_key else (),
            health_path=DEFAULT_MCP_HEALTH_PATH,
            ready_path=DEFAULT_MCP_READY_PATH,
            config_path=DEFAULT_MCP_CONFIG_PATH,
            ready_checks=("models",),
        )
    )


def __getattr__(name: str):
    if name == "app":
        return create_http_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run Grok Search MCP over stdio or HTTP.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http"),
        default=config.mcp_transport,
        help="MCP transport mode",
    )
    parser.add_argument("--host", default=config.mcp_http_host, help="HTTP bind host")
    parser.add_argument("--port", type=int, default=config.mcp_http_port, help="HTTP bind port")
    parser.add_argument("--path", default=config.mcp_http_path, help="HTTP MCP path")
    return parser.parse_args(argv)


def _install_signal_handlers():
    import os
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        return

    def handle_shutdown(_signum, _frame):
        os._exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, handle_shutdown)


def _start_windows_parent_monitor():
    import ctypes
    import os
    import threading

    if sys.platform != "win32":
        return

    parent_pid = os.getppid()

    def is_parent_alive(pid):
        """Windows 下检查进程是否存活。"""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True
        exit_code = ctypes.c_ulong()
        result = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return result and exit_code.value == STILL_ACTIVE

    def monitor_parent():
        while True:
            if not is_parent_alive(parent_pid):
                os._exit(0)
            time.sleep(2)

    threading.Thread(target=monitor_parent, daemon=True).start()


def _run_stdio():
    import os

    _start_windows_parent_monitor()
    try:
        mcp.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        pass
    finally:
        os._exit(0)


def _run_http(host: str, port: int, path: str):
    import uvicorn

    http_app = create_http_app(mcp_path=path)
    uvicorn.run(
        http_app,
        host=host,
        port=port,
        log_level=config.log_level.lower(),
    )


def main(argv=None):
    args = _parse_args(argv)
    _install_signal_handlers()
    if args.transport in {"http", "streamable-http"}:
        _run_http(args.host, args.port, args.path)
        return
    _run_stdio()


def main_http():
    main(["--transport", "http"])


if __name__ == "__main__":
    main()
