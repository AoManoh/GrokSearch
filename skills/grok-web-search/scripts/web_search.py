#!/usr/bin/env python3
"""grok-web-search: AI-powered web search via Grok API.

Independent skill — no dependency on the GrokSearch package or MCP runtime.
Reads only environment variables. See ../SKILL.md for usage.

This file is built up across multiple commits. This first revision contains
the pure (synchronous, no I/O) helpers: time-context injection detection
and source extraction (port of `split_answer_and_sources` from
`src/grok_search/sources.py`).
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

import httpx  # noqa: F401  (used in later revisions; importing now keeps deps explicit)


# ---- Time-context injection ----------------------------------------------

_CN_TIME_KEYWORDS = (
    "当前", "现在", "今天", "明天", "昨天",
    "本周", "上周", "下周", "这周",
    "本月", "上月", "下月", "这个月",
    "今年", "去年", "明年",
    "最新", "最近", "近期", "刚刚", "刚才",
    "实时", "即时", "目前",
)

_EN_TIME_KEYWORDS = (
    "current", "now", "today", "tomorrow", "yesterday",
    "this week", "last week", "next week",
    "this month", "last month", "next month",
    "this year", "last year", "next year",
    "latest", "recent", "recently", "just now",
    "real-time", "realtime", "up-to-date",
)

_WEEKDAYS_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def needs_time_context(query: str) -> bool:
    if not query:
        return False
    if any(k in query for k in _CN_TIME_KEYWORDS):
        return True
    lower = query.lower()
    return any(k in lower for k in _EN_TIME_KEYWORDS)


def get_local_time_info() -> str:
    try:
        local_tz = datetime.now().astimezone().tzinfo
        now = datetime.now(local_tz)
    except Exception:
        now = datetime.now(timezone.utc)
    weekday = _WEEKDAYS_CN[now.weekday()]
    return (
        "[Current Time Context]\n"
        f"- Date: {now.strftime('%Y-%m-%d')} ({weekday})\n"
        f"- Time: {now.strftime('%H:%M:%S')}\n"
        f"- Timezone: {now.tzname() or 'Local'}\n"
    )


# ---- Source extraction (port of split_answer_and_sources) ----------------

_URL_PATTERN = re.compile(r'https?://[^\s<>"\'`，。、；：！？》）】\)]+')
_MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_INLINE_CITATION_PATTERN = re.compile(r"\[\[(\d+)\]\]\((https?://[^)\s]+)\)")
_SOURCES_HEADING_PATTERN = re.compile(
    r"(?im)^"
    r"(?:#{1,6}\s*)?"
    r"(?:\*\*|__)?\s*"
    r"(sources?|references?|citations?|信源|参考资料|参考|引用|来源列表|来源)"
    r"\s*(?:\*\*|__)?"
    r"(?:\s*[（(][^)\n]*[)）])?"
    r"\s*[:：]?\s*$"
)
_SOURCES_FUNCTION_PATTERN = re.compile(
    r"(?im)(^|\n)\s*(sources|source|citations|citation|references|reference|citation_card|source_cards|source_card)\s*\("
)


def extract_unique_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_PATTERN.finditer(text or ""):
        u = m.group().rstrip(".,;:!?")
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def is_link_only_line(line: str) -> bool:
    stripped = re.sub(r"^\s*(?:[-*]|\d+\.)\s*", "", line).strip()
    if not stripped:
        return False
    if stripped.startswith(("http://", "https://")):
        return True
    if _MD_LINK_PATTERN.search(stripped):
        return True
    return False


def split_answer_and_sources(text: str) -> tuple[str, list[dict]]:
    raw = (text or "").strip()
    if not raw:
        return "", []
    for strategy in (
        _split_function_call_sources,
        _split_heading_sources,
        _split_details_block_sources,
        _split_tail_link_block,
        _split_inline_citations,
    ):
        result = strategy(raw)
        if result is not None:
            return result
    return raw, []


def _split_function_call_sources(text: str) -> tuple[str, list[dict]] | None:
    matches = list(_SOURCES_FUNCTION_PATTERN.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        open_paren_idx = m.end() - 1
        extracted = _extract_balanced_call_at_end(text, open_paren_idx)
        if not extracted:
            continue
        _, args_text = extracted
        sources = _parse_sources_payload(args_text)
        if not sources:
            continue
        answer = text[: m.start()].rstrip()
        return answer, sources
    return None


def _extract_balanced_call_at_end(text: str, open_paren_idx: int) -> tuple[int, str] | None:
    if open_paren_idx < 0 or open_paren_idx >= len(text) or text[open_paren_idx] != "(":
        return None
    depth = 1
    in_string: str | None = None
    escape = False
    for idx in range(open_paren_idx + 1, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == in_string:
                in_string = None
            continue
        if ch in ("'", '"'):
            in_string = ch
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                if text[idx + 1:].strip():
                    return None
                return idx, text[open_paren_idx + 1: idx]
    return None


def _split_heading_sources(text: str) -> tuple[str, list[dict]] | None:
    matches = list(_SOURCES_HEADING_PATTERN.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        start = m.start()
        sources_text = text[start:]
        sources = _extract_sources_from_text(sources_text)
        if not sources:
            continue
        return text[:start].rstrip(), sources
    return None


def _split_tail_link_block(text: str) -> tuple[str, list[dict]] | None:
    lines = text.splitlines()
    if not lines:
        return None
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return None
    tail_end = idx
    link_like_count = 0
    while idx >= 0:
        line = lines[idx].strip()
        if not line:
            idx -= 1
            continue
        if not is_link_only_line(line):
            break
        link_like_count += 1
        idx -= 1
    tail_start = idx + 1
    if link_like_count < 2:
        return None
    block_text = "\n".join(lines[tail_start: tail_end + 1])
    sources = _extract_sources_from_text(block_text)
    if not sources:
        return None
    answer = "\n".join(lines[:tail_start]).rstrip()
    return answer, sources


def _split_inline_citations(text: str) -> tuple[str, list[dict]] | None:
    sources: list[dict] = []
    seen: set[str] = set()
    for m in _INLINE_CITATION_PATTERN.finditer(text or ""):
        url = m.group(2).strip().rstrip(".,;:!?")
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({"url": url})
    if not sources:
        return None
    return text, sources


def _split_details_block_sources(text: str) -> tuple[str, list[dict]] | None:
    lower = text.lower()
    close_idx = lower.rfind("</details>")
    if close_idx == -1:
        return None
    tail = text[close_idx + len("</details>"):].strip()
    if tail:
        return None
    open_idx = lower.rfind("<details", 0, close_idx)
    if open_idx == -1:
        return None
    block_text = text[open_idx: close_idx + len("</details>")]
    sources = _extract_sources_from_text(block_text)
    if len(sources) < 2:
        return None
    return text[:open_idx].rstrip(), sources


def _parse_sources_payload(payload: str) -> list[dict]:
    payload = (payload or "").strip().rstrip(";")
    if not payload:
        return []
    data: Any = None
    try:
        data = json.loads(payload)
    except Exception:
        try:
            data = ast.literal_eval(payload)
        except Exception:
            data = None
    if data is None:
        return _extract_sources_from_text(payload)
    if isinstance(data, dict):
        for key in ("sources", "citations", "references", "urls"):
            if key in data:
                return _normalize_sources(data[key])
        return _normalize_sources(data)
    return _normalize_sources(data)


def _normalize_sources(data: Any) -> list[dict]:
    if isinstance(data, (list, tuple)):
        items = list(data)
    else:
        items = [data]
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            for url in extract_unique_urls(item):
                if url not in seen:
                    seen.add(url)
                    out.append({"url": url})
            continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            title, url = item[0], item[1]
            if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                rec: dict = {"url": url}
                if isinstance(title, str) and title.strip():
                    rec["title"] = title.strip()
                out.append(rec)
            continue
        if isinstance(item, dict):
            url = item.get("url") or item.get("href") or item.get("link")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            rec = {"url": url}
            title = item.get("title") or item.get("name") or item.get("label")
            if isinstance(title, str) and title.strip():
                rec["title"] = title.strip()
            desc = item.get("description") or item.get("snippet") or item.get("content")
            if isinstance(desc, str) and desc.strip():
                rec["description"] = desc.strip()
            out.append(rec)
    return out


def _extract_sources_from_text(text: str) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    for title, url in _MD_LINK_PATTERN.findall(text or ""):
        url = (url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = (title or "").strip()
        sources.append({"title": title, "url": url} if title else {"url": url})
    for url in extract_unique_urls(text or ""):
        if url in seen:
            continue
        seen.add(url)
        sources.append({"url": url})
    return sources


# ============================================================================
# HTTP layer
# ============================================================================
import asyncio
import os
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlparse

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

_STATUS_PATTERNS = (
    re.compile(r"\bHTTP\s+([1-5]\d{2})\b", re.IGNORECASE),
    re.compile(r"\bstatus(?:=|:)?\s*([1-5]\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(?:failed|error|redirect(?:ed)?)\b[^0-9]{0,12}([1-5]\d{2})\b", re.IGNORECASE),
    re.compile(r",\s*([1-5]\d{2})(?:\D|$)"),
)


class UpstreamSSEError(RuntimeError):
    def __init__(self, message: str, *, upstream_status: int | None = None):
        super().__init__(message)
        self.upstream_status = upstream_status
        self.retryable = upstream_status in RETRYABLE_STATUS_CODES if upstream_status else False


class ProxyConfigurationError(RuntimeError):
    pass


_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)
_HTTP_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_SOCKS_SCHEMES = {"socks", "socks4", "socks4a", "socks5", "socks5h"}


def _proxy_scheme(value: str | None) -> str:
    return (urlparse(value or "").scheme or "").lower()


def _has_http_proxy_fallback(env: dict[str, str]) -> bool:
    return any(_proxy_scheme(env.get(key)) in {"http", "https"} for key in _HTTP_PROXY_ENV_KEYS)


def sanitize_proxy_environment(env: dict[str, str] | None = None) -> list[str]:
    """Remove invalid fallback SOCKS proxy env vars before httpx reads them."""
    target = env if env is not None else os.environ
    warnings: list[str] = []
    has_http_fallback = _has_http_proxy_fallback(target)
    has_socksio = importlib.util.find_spec("socksio") is not None

    for key in _PROXY_ENV_KEYS:
        value = target.get(key)
        if not value:
            continue
        scheme = _proxy_scheme(value)
        if scheme not in _SOCKS_SCHEMES:
            continue

        if key.lower() == "all_proxy" and has_http_fallback:
            target.pop(key, None)
            warnings.append(
                f"{key} used SOCKS proxy {value!r}; ignored because HTTP_PROXY/HTTPS_PROXY is available."
            )
            continue

        if scheme == "socks":
            raise ProxyConfigurationError(
                f"{key}={value!r} uses unsupported socks:// proxy syntax. "
                "Use socks5:// with `pip install httpx[socks]`, use an http:// proxy, or clear the variable."
            )
        if not has_socksio:
            raise ProxyConfigurationError(
                f"{key}={value!r} requires SOCKS support. "
                "Install `httpx[socks]`, use an http:// proxy, or clear the variable."
            )

    return warnings


def extract_status_from_message(message: str) -> Optional[int]:
    for pattern in _STATUS_PATTERNS:
        m = pattern.search(message or "")
        if not m:
            continue
        code = int(m.group(1))
        if 300 <= code <= 599:
            return code
    return None


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    if isinstance(exc, UpstreamSSEError):
        return exc.retryable
    return False


def _retry_wait_seconds(exc: Exception, attempt: int, *, multiplier: float = 1.0, max_wait: float = 10.0) -> float:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        ra = (exc.response.headers.get("Retry-After") or "").strip()
        if ra.isdigit():
            return float(ra)
        try:
            dt = parsedate_to_datetime(ra)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            pass
    base = min((2 ** attempt) * multiplier, max_wait)
    if isinstance(exc, httpx.RemoteProtocolError):
        base += 3.0
    return base


# ---- SSE parsing ---------------------------------------------------------

async def parse_streaming_response(response) -> str:
    content = ""
    full_buffer: list[str] = []
    current_event = "message"

    async for line in response.aiter_lines():
        line = line.strip()
        if not line:
            continue
        full_buffer.append(line)

        if line.startswith("event:"):
            current_event = line[6:].strip() or "message"
            continue

        if line.startswith("data:"):
            if line in ("data: [DONE]", "data:[DONE]"):
                break
            try:
                json_str = line[5:].lstrip()
                data = json.loads(json_str)
            except (json.JSONDecodeError, IndexError):
                continue

            if current_event == "error" or "error" in data:
                err = data.get("error") or {}
                if isinstance(err, dict):
                    msg = str(err.get("message") or err).strip()
                    raw_status = err.get("status") or err.get("upstream_status")
                else:
                    msg = str(err).strip()
                    raw_status = None
                upstream_status: Optional[int] = None
                if isinstance(raw_status, int) and 300 <= raw_status <= 599:
                    upstream_status = raw_status
                elif isinstance(raw_status, str):
                    upstream_status = extract_status_from_message(raw_status)
                if upstream_status is None:
                    upstream_status = extract_status_from_message(msg)
                raise UpstreamSSEError(msg or "Unknown upstream SSE error", upstream_status=upstream_status)

            choices = data.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                if "content" in delta and isinstance(delta["content"], str):
                    content += delta["content"]
            current_event = "message"

    if not content and full_buffer:
        try:
            data = json.loads("".join(full_buffer))
            if isinstance(data, dict):
                choices = data.get("choices") or []
                if choices:
                    msg = choices[0].get("message") or {}
                    if isinstance(msg.get("content"), str):
                        content = msg["content"]
        except json.JSONDecodeError:
            pass

    return content


# ---- Provider routing ----------------------------------------------------

def resolve_provider_mode(api_url: str) -> str:
    mode = os.environ.get("GROK_SEARCH_PROVIDER", "auto").strip().lower() or "auto"
    if mode in ("chat", "responses"):
        return mode
    hostname = (urlparse(api_url).hostname or "").lower()
    if hostname == "api.x.ai" or hostname.endswith(".api.x.ai"):
        return "responses"
    return "chat"


# ---- Chat-mode call (SSE) ------------------------------------------------

SEARCH_SYSTEM_PROMPT = (
    "You are a precise web-search assistant. Cite every claim you make. "
    "Prefer authoritative primary sources. End the answer with a `## Sources` "
    "section listing every URL referenced inline, formatted as Markdown links."
)


async def call_grok_chat(
    *, api_url: str, api_key: str, model: str, query: str, platform: str = "",
    timeout: int = 120, max_attempts: int = 4,
) -> str:
    user_content = query
    if platform:
        user_content += "\n\nFocus on these platforms: " + platform
    if needs_time_context(query):
        user_content = get_local_time_info() + "\n" + user_content

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_exc: Exception | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=6.0, read=float(timeout), write=10.0, pool=None),
        follow_redirects=True,
    ) as client:
        for attempt in range(max_attempts):
            try:
                async with client.stream(
                    "POST", f"{api_url.rstrip('/')}/chat/completions",
                    headers=headers, json=payload,
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                    resp.raise_for_status()
                    return await parse_streaming_response(resp)
            except Exception as exc:
                last_exc = exc
                if not is_retryable(exc) or attempt >= max_attempts - 1:
                    raise
                await asyncio.sleep(_retry_wait_seconds(exc, attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop exited unexpectedly")


# ============================================================================
# Responses-mode (xai /responses) payload parsing
# ============================================================================

_SOURCE_URL_KEYS = ("url", "uri", "href", "link")
_SOURCE_TITLE_KEYS = ("title", "name", "label")
_SOURCE_DESC_KEYS = ("description", "snippet", "content")


def _stringify_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_stringify_text(v) for v in value)
    if isinstance(value, dict):
        return _stringify_text(value.get("text") or value.get("content"))
    return ""


def _extract_responses_output_text(data: dict) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "message"):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") not in (None, "output_text", "text"):
                continue
            text = _stringify_text(content)
            if text:
                parts.append(text)
    if parts:
        return "".join(parts).strip()
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, str):
            return content.strip()
    return ""


def _extract_url_from_source(item: dict) -> str:
    for key in _SOURCE_URL_KEYS:
        v = item.get(key)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v.strip()
    for key in ("web_citation", "x_citation", "citation", "source"):
        nested = item.get(key)
        if isinstance(nested, dict):
            v = _extract_url_from_source(nested)
            if v:
                return v
    return ""


def _normalize_source_item(item: Any) -> list[dict]:
    if isinstance(item, str):
        return [{"url": u} for u in extract_unique_urls(item)]
    if not isinstance(item, dict):
        return []
    url = _extract_url_from_source(item)
    if not url:
        return []
    out: dict = {"url": url}
    for key in _SOURCE_TITLE_KEYS:
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            out["title"] = v.strip()
            break
    for key in _SOURCE_DESC_KEYS:
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            out["description"] = v.strip()
            break
    return [out]


def _extract_responses_sources(data: dict) -> list[dict]:
    sources: list[dict] = []
    for citation in data.get("citations") or []:
        sources.extend(_normalize_source_item(citation))
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            for ann in content.get("annotations") or []:
                sources.extend(_normalize_source_item(ann))
    for c in data.get("inline_citations") or []:
        sources.extend(_normalize_source_item(c))
    return sources


def merge_sources(*lists: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for lst in lists:
        for item in lst or []:
            url = (item or {}).get("url")
            if not isinstance(url, str) or not url.strip() or url in seen:
                continue
            seen.add(url)
            merged.append(item)
    return merged


def parse_responses_payload(data: dict) -> tuple[str, list[dict], str]:
    """Return (answer, sources, model) from a /responses API payload."""
    answer_raw = _extract_responses_output_text(data)
    answer, inline_sources = split_answer_and_sources(answer_raw)
    structured = _extract_responses_sources(data)
    sources = merge_sources(structured, inline_sources)
    model = str(data.get("model") or "")
    return answer, sources, model


# ---- Responses-mode call -------------------------------------------------

async def call_grok_responses(
    *, api_url: str, api_key: str, model: str, query: str, platform: str = "",
    timeout: int = 120, max_attempts: int = 4,
) -> dict:
    user_content = get_local_time_info() + "\n" + query
    if platform:
        user_content += "\n\nFocus on these platforms: " + platform

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "tools": [{"type": "web_search"}],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_exc: Exception | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=6.0, read=float(timeout), write=10.0, pool=None),
        follow_redirects=True,
    ) as client:
        for attempt in range(max_attempts):
            try:
                resp = await client.post(
                    f"{api_url.rstrip('/')}/responses",
                    headers=headers, json=payload,
                )
                resp.raise_for_status()
                return resp.json() or {}
            except Exception as exc:
                last_exc = exc
                if not is_retryable(exc) or attempt >= max_attempts - 1:
                    raise
                await asyncio.sleep(_retry_wait_seconds(exc, attempt))
    if last_exc:
        raise last_exc
    return {}


# ============================================================================
# Extra sources (Tavily Search + Firecrawl Search)
# ============================================================================

def compute_extra_quota(*, extra: int, has_firecrawl: bool, has_tavily: bool) -> tuple[int, int]:
    """Mirror the MCP `web_search` quota split exactly.

    When both providers are available, all of `extra` goes to Firecrawl
    (this matches the upstream `firecrawl_count = round(extra * 1)` line).
    """
    if extra <= 0:
        return 0, 0
    if has_firecrawl and has_tavily:
        return extra, 0
    if has_firecrawl:
        return extra, 0
    if has_tavily:
        return 0, extra
    return 0, 0


async def call_tavily_search(query: str, max_results: int) -> list[dict] | None:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key or max_results <= 0:
        return None
    api_url = os.environ.get("TAVILY_API_URL", "https://api.tavily.com").rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"query": query, "max_results": max_results}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{api_url}/search", headers=headers, json=body)
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
    except Exception:
        return None
    out: list[dict] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        rec: dict = {"url": url, "provider": "tavily"}
        title = (item.get("title") or "").strip()
        if title:
            rec["title"] = title
        desc = (item.get("content") or "").strip()
        if desc:
            rec["description"] = desc
        out.append(rec)
    return out


async def call_firecrawl_search(query: str, limit: int) -> list[dict] | None:
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key or limit <= 0:
        return None
    api_url = os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v2").rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"query": query, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{api_url}/search", headers=headers, json=body)
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
    except Exception:
        return None
    out: list[dict] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        rec: dict = {"url": url, "provider": "firecrawl"}
        title = (item.get("title") or "").strip()
        if title:
            rec["title"] = title
        desc = (item.get("description") or "").strip()
        if desc:
            rec["description"] = desc
        out.append(rec)
    return out


# ============================================================================
# CLI orchestrator
# ============================================================================
import argparse


DEFAULT_MODEL = "grok-4.1-fast"
PREFERRED_MODEL_IDS = (
    "grok-4.20-fast",
    "grok-4.1-fast",
)


def _is_text_model(model_id: str) -> bool:
    lowered = model_id.lower()
    if not lowered.startswith("grok-"):
        return False
    return not any(marker in lowered for marker in ("image", "imagine", "vision", "embedding", "audio", "tts"))


def select_default_model(model_ids: list[str]) -> str:
    cleaned = [model_id.strip() for model_id in model_ids if model_id and model_id.strip()]
    for preferred in PREFERRED_MODEL_IDS:
        if preferred in cleaned:
            return preferred
    for model_id in cleaned:
        if _is_text_model(model_id) and "fast" in model_id.lower():
            return model_id
    for model_id in cleaned:
        if _is_text_model(model_id):
            return model_id
    return DEFAULT_MODEL


async def fetch_model_ids(*, api_url: str, api_key: str, timeout: int = 30) -> list[str]:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=6.0, read=float(timeout), write=10.0, pool=None),
        follow_redirects=True,
    ) as client:
        resp = await client.get(f"{api_url.rstrip('/')}/models", headers=headers)
        resp.raise_for_status()
        data = resp.json() or {}
    out: list[str] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            out.append(model_id.strip())
    return out


async def resolve_default_model(*, api_url: str, api_key: str, timeout: int = 30) -> str:
    try:
        model_ids = await fetch_model_ids(api_url=api_url, api_key=api_key, timeout=timeout)
    except Exception:
        return DEFAULT_MODEL
    return select_default_model(model_ids)


def format_http_status_error(exc: httpx.HTTPStatusError, *, model: str) -> str:
    response = exc.response
    status = response.status_code if response is not None else "unknown"
    url = str(response.request.url) if response is not None and response.request is not None else "unknown URL"
    message = f"HTTP {status} from {url}: {str(exc) or exc.__class__.__name__}"
    body = ""
    if response is not None:
        try:
            body = (response.text or "").strip()
        except httpx.ResponseNotRead:
            body = "Response body was not read before the upstream HTTP error was raised."
    if body:
        if len(body) > 2000:
            body = body[:2000].rstrip() + "...(truncated)"
        message += f"\nUpstream response body: {body}"
    lowered = f"{message}\n{body}".lower()
    if model and (status == 400 or "model_not_found" in lowered or "model '" in lowered and "does not exist" in lowered):
        message += (
            f"\nHint: model {model!r} is not available for this endpoint. "
            "Run `--list-models` or set `--model` / `GROK_MODEL` to an available model."
        )
    return message


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web_search.py",
        description="AI-powered web search via Grok (OpenAI-compatible chat or xai /responses).",
    )
    p.add_argument("--query", default="", help="Search query. Required unless --list-models is used.")
    p.add_argument("--platform", default="", help='Focus platform(s), e.g. "GitHub, Reddit".')
    p.add_argument("--model", default="", help="Model ID override. Defaults to env GROK_MODEL or auto-selected /models result.")
    p.add_argument("--extra-sources", type=int, default=0, help="Tavily/Firecrawl Search supplements (0 = off).")
    p.add_argument("--timeout", type=int, default=120, help="Per-request read timeout (seconds).")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    p.add_argument("--list-models", action="store_true", help="List available model IDs for GROK_API_URL and exit.")
    return p


def _format_markdown(answer: str, sources: list[dict], *, provider: str, model: str) -> str:
    lines = ["## Answer", "", answer.strip() if answer else "_(empty answer)_"]
    if sources:
        lines += ["", "## Sources", ""]
        for i, s in enumerate(sources, 1):
            url = s.get("url", "")
            title = s.get("title") or url
            desc = s.get("description")
            line = f"{i}. [{title}]({url})"
            if desc:
                line += f" — {desc}"
            lines.append(line)
    lines += ["", "---", f"provider: {provider}", f"model: {model}"]
    return "\n".join(lines)


async def _run_search(args, *, api_url: str, api_key: str, model: str) -> dict:
    """Top-level orchestration. Returns a result dict for CLI rendering."""
    provider_mode = resolve_provider_mode(api_url)

    # Run Grok + extra-sources in parallel.
    has_tavily = bool(os.environ.get("TAVILY_API_KEY", "").strip())
    has_firecrawl = bool(os.environ.get("FIRECRAWL_API_KEY", "").strip())
    fc_count, tv_count = compute_extra_quota(
        extra=max(0, args.extra_sources),
        has_firecrawl=has_firecrawl,
        has_tavily=has_tavily,
    )

    async def _grok():
        if provider_mode == "responses":
            data = await call_grok_responses(
                api_url=api_url, api_key=api_key, model=model,
                query=args.query, platform=args.platform, timeout=args.timeout,
            )
            answer, sources, effective_model = parse_responses_payload(data)
            return answer, sources, (effective_model or model)
        raw = await call_grok_chat(
            api_url=api_url, api_key=api_key, model=model,
            query=args.query, platform=args.platform, timeout=args.timeout,
        )
        answer, sources = split_answer_and_sources(raw)
        return answer, sources, model

    async def _safe_tavily():
        try:
            return await call_tavily_search(args.query, max_results=tv_count)
        except Exception:
            return None

    async def _safe_firecrawl():
        try:
            return await call_firecrawl_search(args.query, limit=fc_count)
        except Exception:
            return None

    coros = [_grok()]
    if tv_count > 0:
        coros.append(_safe_tavily())
    if fc_count > 0:
        coros.append(_safe_firecrawl())

    gathered = await asyncio.gather(*coros, return_exceptions=True)

    grok_outcome = gathered[0]
    idx = 1
    tv_results = gathered[idx] if tv_count > 0 else None
    if tv_count > 0:
        idx += 1
    fc_results = gathered[idx] if fc_count > 0 else None

    if isinstance(grok_outcome, Exception):
        raise grok_outcome
    answer, grok_sources, effective_model = grok_outcome

    extra: list[dict] = []
    if isinstance(fc_results, list):
        extra.extend(fc_results)
    if isinstance(tv_results, list):
        extra.extend(tv_results)

    sources = merge_sources(grok_sources, extra)
    return {
        "answer": answer,
        "sources": sources,
        "provider": provider_mode,
        "model": effective_model,
    }


_MISSING_URL_HINT = (
    "GROK_API_URL is not set. Export it (`export GROK_API_URL=...`) or "
    "load a .env file via `set -a; source .env; set +a` before invoking. "
    "Examples: https://api.x.ai/v1 (official) or any OpenAI-compatible Grok proxy."
)
_MISSING_KEY_HINT = (
    "GROK_API_KEY is not set. Export it (`export GROK_API_KEY=...`) or "
    "load a .env file via `set -a; source .env; set +a` before invoking. "
    "Get a key at https://console.x.ai/."
)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    api_url = os.environ.get("GROK_API_URL", "").strip()
    api_key = os.environ.get("GROK_API_KEY", "").strip()
    if not api_url:
        if args.json:
            print(json.dumps({"status": "error", "error": {"code": "config_error", "message": _MISSING_URL_HINT}}))
        else:
            print(f"error: {_MISSING_URL_HINT}", file=sys.stderr)
        return 1
    if not api_key:
        if args.json:
            print(json.dumps({"status": "error", "error": {"code": "config_error", "message": _MISSING_KEY_HINT}}))
        else:
            print(f"error: {_MISSING_KEY_HINT}", file=sys.stderr)
        return 1

    try:
        sanitize_proxy_environment()
    except ProxyConfigurationError as exc:
        msg = str(exc)
        if args.json:
            print(json.dumps({"status": "error", "error": {"code": "config_error", "message": msg}}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1

    if args.list_models:
        try:
            model_ids = asyncio.run(fetch_model_ids(api_url=api_url, api_key=api_key, timeout=args.timeout))
        except httpx.HTTPStatusError as exc:
            msg = format_http_status_error(exc, model="")
            if args.json:
                print(json.dumps({"status": "error", "error": {"code": "upstream_error", "message": msg}}))
            else:
                print(f"error: {msg}", file=sys.stderr)
            return 2
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            msg = str(exc) or exc.__class__.__name__
            if args.json:
                print(json.dumps({"status": "error", "error": {"code": "network_error", "message": msg}}))
            else:
                print(f"error: network failure: {msg}", file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps({"status": "ok", "models": model_ids}, ensure_ascii=False))
        else:
            for model_id in model_ids:
                print(model_id)
        return 0

    if not args.query.strip():
        msg = "--query is required unless --list-models is used."
        if args.json:
            print(json.dumps({"status": "error", "error": {"code": "config_error", "message": msg}}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1

    explicit_model = (args.model or os.environ.get("GROK_MODEL", "")).strip()
    model = explicit_model or asyncio.run(resolve_default_model(api_url=api_url, api_key=api_key, timeout=30))

    try:
        result = asyncio.run(_run_search(args, api_url=api_url, api_key=api_key, model=model))
    except UpstreamSSEError as exc:
        msg = str(exc) or exc.__class__.__name__
        body = {"status": "error", "error": {"code": "upstream_error", "message": msg}}
        if exc.upstream_status:
            body["error"]["upstream_status"] = exc.upstream_status
        if args.json:
            print(json.dumps(body))
        else:
            print(f"error: upstream sse error: {msg}", file=sys.stderr)
        return 2
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        msg = format_http_status_error(exc, model=model)
        body = {"status": "error", "error": {"code": "upstream_error", "message": msg}}
        if status is not None:
            body["error"]["upstream_status"] = status
        if args.json:
            print(json.dumps(body))
        else:
            print(f"error: upstream {status}: {msg}", file=sys.stderr)
        return 2
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        msg = str(exc) or exc.__class__.__name__
        if args.json:
            print(json.dumps({"status": "error", "error": {"code": "network_error", "message": msg}}))
        else:
            print(f"error: network failure: {msg}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps({
            "status": "ok",
            "provider": result["provider"],
            "model": result["model"],
            "answer": result["answer"],
            "sources": result["sources"],
        }, ensure_ascii=False))
    else:
        print(_format_markdown(
            result["answer"], result["sources"],
            provider=result["provider"], model=result["model"],
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
