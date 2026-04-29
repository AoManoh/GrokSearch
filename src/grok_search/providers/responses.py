import json
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from .base import BaseSearchProvider, SearchResponse
from .grok import _WaitWithRetryAfter, _is_retryable_exception, get_local_time_info
from ..config import config
from ..logger import log_info
from ..sources import merge_sources, split_answer_and_sources
from ..utils import extract_unique_urls, search_prompt


_SOURCE_URL_KEYS = ("url", "uri", "href", "link")
_SOURCE_TITLE_KEYS = ("title", "name", "label")
_SOURCE_DESCRIPTION_KEYS = ("description", "snippet", "content")


def _stringify_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_stringify_text(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        return _stringify_text(text)
    return ""


def _extract_output_text(data: dict[str, Any]) -> str:
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


def _extract_url_from_source(item: dict[str, Any]) -> str:
    for key in _SOURCE_URL_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.strip()

    for key in ("web_citation", "x_citation", "citation", "source"):
        nested = item.get(key)
        if isinstance(nested, dict):
            value = _extract_url_from_source(nested)
            if value:
                return value
    return ""


def _normalize_source_item(item: Any) -> list[dict]:
    if isinstance(item, str):
        return [{"url": url} for url in extract_unique_urls(item)]

    if not isinstance(item, dict):
        return []

    url = _extract_url_from_source(item)
    if not url:
        return []

    out: dict[str, Any] = {"url": url}
    for key in _SOURCE_TITLE_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            out["title"] = value.strip()
            break
    for key in _SOURCE_DESCRIPTION_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            out["description"] = value.strip()
            break
    return [out]


def _extract_annotations(data: dict[str, Any]) -> list[dict]:
    sources: list[dict] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations") or []:
                sources.extend(_normalize_source_item(annotation))
    return sources


def _extract_inline_citations(data: dict[str, Any]) -> list[dict]:
    sources: list[dict] = []
    for citation in data.get("inline_citations") or []:
        sources.extend(_normalize_source_item(citation))
    return sources


def _extract_top_level_citations(data: dict[str, Any]) -> list[dict]:
    sources: list[dict] = []
    for citation in data.get("citations") or []:
        sources.extend(_normalize_source_item(citation))
    return sources


def parse_responses_api_payload(data: dict[str, Any], *, provider: str = "xai-responses", model: str = "") -> SearchResponse:
    answer = _extract_output_text(data)
    answer_from_inline, inline_sources = split_answer_and_sources(answer)
    answer = answer_from_inline
    sources = merge_sources(
        _extract_top_level_citations(data),
        _extract_annotations(data),
        _extract_inline_citations(data),
        inline_sources,
    )
    return SearchResponse(
        answer=answer,
        sources=sources,
        raw_content=answer,
        raw_metadata={"id": data.get("id"), "usage": data.get("usage") or {}},
        provider=provider,
        model=model or str(data.get("model") or ""),
    )


class ResponsesSearchProvider(BaseSearchProvider):
    def __init__(self, api_url: str, api_key: str, model: str = "grok-4.20-reasoning"):
        super().__init__(api_url, api_key)
        self.model = model

    def get_provider_name(self) -> str:
        return "xai-responses"

    async def search(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None) -> str:
        response = await self.search_response(query, platform, min_results, max_results, ctx)
        return response.answer

    async def search_response(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None) -> SearchResponse:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        platform_prompt = ""
        if platform:
            platform_prompt = "\n\nYou should search the web for the information you need, and focus on these platform: " + platform + "\n"

        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": search_prompt},
                {"role": "user", "content": get_local_time_info() + "\n" + query + platform_prompt},
            ],
            "tools": [{"type": "web_search"}],
        }
        await log_info(ctx, f"responses_payload: {json.dumps({'model': self.model, 'query': query, 'platform': platform}, ensure_ascii=False)}", config.debug_enabled)
        data = await self._execute_with_retry(headers, payload)
        response = parse_responses_api_payload(data, provider=self.get_provider_name(), model=self.model)
        await log_info(ctx, f"responses_content: {response.answer}", config.debug_enabled)
        return response

    async def _execute_with_retry(self, headers: dict, payload: dict) -> dict[str, Any]:
        timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(config.retry_max_attempts + 1),
                wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                retry=retry_if_exception(_is_retryable_exception),
                reraise=True,
            ):
                with attempt:
                    response = await client.post(
                        f"{self.api_url.rstrip('/')}/responses",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    return response.json()
        return {}
