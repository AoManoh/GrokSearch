import asyncio
import httpx
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base
from zoneinfo import ZoneInfo
from .base import BaseSearchProvider, SearchResponse, SearchResult
from ._concurrency import get_grok_semaphore, maybe_acquire_grok_semaphore
from ..utils import search_prompt, fetch_prompt, url_describe_prompt, rank_sources_prompt
from ..logger import log_info
from ..config import config
from ..sources import split_answer_and_sources


# 进程级共享的 httpx.AsyncClient，用于复用 TCP/TLS 连接池。
#
# 早先版本在 _execute_stream_with_retry 内部 `async with httpx.AsyncClient(...)`
# 每次新建 client，每个 query 都要重新做 DNS / TCP / TLS 握手，单 query 增加
# 100-300ms 固定开销；高 batch 场景下握手时间被串行化，等效降低有效并发度。
#
# 改动后：模块级一个 AsyncClient 单例，共享连接池 + HTTP/2 多路复用（如果上游
# 协商成功）。timeout 与 limits 在创建时设置好，业务侧不再重复传。
#
# 生命周期：进程退出时 asyncio 会自动回收；显式 close 由 reset_shared_client
# 提供给测试。
_SHARED_CLIENT: httpx.AsyncClient | None = None
# 记录 client 关联的 event loop，用于跨 loop 测试场景下判定是否需要重建。
# 生产环境只有一个 loop，所以这里几乎是空开销；测试每个用例都有独立 loop，
# 不做这个保护就会触发 "Event loop is closed" 类型的伪故障。
_SHARED_CLIENT_LOOP_ID: int | None = None
_SHARED_CLIENT_LOCK: asyncio.Lock | None = None
_DEFAULT_HTTPX_TIMEOUT = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=30.0)
_DEFAULT_HTTPX_LIMITS = httpx.Limits(
    max_connections=64,
    max_keepalive_connections=32,
    keepalive_expiry=30.0,
)


def _current_loop_id() -> int:
    return id(asyncio.get_running_loop())


def _get_shared_client_lock() -> asyncio.Lock:
    """asyncio.Lock 也绑定 loop，所以同样按需懒加载，并在 loop 切换时重建。"""
    global _SHARED_CLIENT_LOCK
    loop_id = _current_loop_id()
    if _SHARED_CLIENT_LOCK is None or getattr(_SHARED_CLIENT_LOCK, "_loop_id", None) != loop_id:
        lock = asyncio.Lock()
        # 注解一下绑定的 loop_id；asyncio.Lock 没有公开 loop 字段，此处用属性补丁
        # 不影响其行为，仅用于跨 loop 判定。
        try:
            object.__setattr__(lock, "_loop_id", loop_id)
        except Exception:
            pass
        _SHARED_CLIENT_LOCK = lock
    return _SHARED_CLIENT_LOCK


async def get_shared_async_client() -> httpx.AsyncClient:
    """返回进程级共享 AsyncClient，按需懒加载。

    多次调用在同一 event loop 内返回同一实例；event loop 切换（例如测试每个
    用例独立 loop）时会自动重建，避免跨 loop 使用同一个 client 触发
    "Event loop is closed"。Semaphore 限流在外层（``_concurrency.py``）独立
    控制业务并发，连接池只是复用底层 socket，与限流互不耦合。
    """
    global _SHARED_CLIENT, _SHARED_CLIENT_LOOP_ID
    loop_id = _current_loop_id()
    if _SHARED_CLIENT is not None and _SHARED_CLIENT_LOOP_ID == loop_id and not _SHARED_CLIENT.is_closed:
        return _SHARED_CLIENT
    lock = _get_shared_client_lock()
    async with lock:
        if (
            _SHARED_CLIENT is None
            or _SHARED_CLIENT_LOOP_ID != loop_id
            or _SHARED_CLIENT.is_closed
        ):
            _SHARED_CLIENT = httpx.AsyncClient(
                timeout=_DEFAULT_HTTPX_TIMEOUT,
                limits=_DEFAULT_HTTPX_LIMITS,
                follow_redirects=True,
            )
            _SHARED_CLIENT_LOOP_ID = loop_id
        return _SHARED_CLIENT


async def reset_shared_async_client() -> None:
    """关闭并丢弃共享 client，仅供测试或显式重新加载使用。

    跨 event loop 的清理是幂等且容错的：如果 client 关联的 loop 已经关闭，
    aclose 可能抛 RuntimeError，这里吞掉，因为底层资源已随 loop 一起释放。
    """
    global _SHARED_CLIENT, _SHARED_CLIENT_LOOP_ID
    client = _SHARED_CLIENT
    _SHARED_CLIENT = None
    _SHARED_CLIENT_LOOP_ID = None
    if client is None or client.is_closed:
        return
    try:
        await client.aclose()
    except RuntimeError:
        # event loop 已关闭等场景下忽略；连接资源会随 loop 一起回收。
        pass


def get_local_time_info() -> str:
    """获取本地时间信息，用于注入到搜索查询中"""
    try:
        # 尝试获取系统本地时区
        local_tz = datetime.now().astimezone().tzinfo
        local_now = datetime.now(local_tz)
    except Exception:
        # 降级使用 UTC
        local_now = datetime.now(timezone.utc)

    # 格式化时间信息
    weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays_cn[local_now.weekday()]

    return (
        f"[Current Time Context]\n"
        f"- Date: {local_now.strftime('%Y-%m-%d')} ({weekday})\n"
        f"- Time: {local_now.strftime('%H:%M:%S')}\n"
        f"- Timezone: {local_now.tzname() or 'Local'}\n"
    )


def _needs_time_context(query: str) -> bool:
    """检查查询是否需要时间上下文"""
    # 中文时间相关关键词
    cn_keywords = [
        "当前", "现在", "今天", "明天", "昨天",
        "本周", "上周", "下周", "这周",
        "本月", "上月", "下月", "这个月",
        "今年", "去年", "明年",
        "最新", "最近", "近期", "刚刚", "刚才",
        "实时", "即时", "目前",
    ]
    # 英文时间相关关键词
    en_keywords = [
        "current", "now", "today", "tomorrow", "yesterday",
        "this week", "last week", "next week",
        "this month", "last month", "next month",
        "this year", "last year", "next year",
        "latest", "recent", "recently", "just now",
        "real-time", "realtime", "up-to-date",
    ]

    query_lower = query.lower()

    for keyword in cn_keywords:
        if keyword in query:
            return True

    for keyword in en_keywords:
        if keyword in query_lower:
            return True

    return False

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

_STATUS_PATTERNS = (
    re.compile(r"\bHTTP\s+([1-5]\d{2})\b", re.IGNORECASE),
    re.compile(r"\bstatus(?:=|:)?\s*([1-5]\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(?:failed|error|redirect(?:ed)?)\b[^0-9]{0,12}([1-5]\d{2})\b", re.IGNORECASE),
    re.compile(r",\s*([1-5]\d{2})(?:\D|$)"),
)


def _extract_status_from_message(message: str) -> Optional[int]:
    for pattern in _STATUS_PATTERNS:
        match = pattern.search(message)
        if not match:
            continue
        status_code = int(match.group(1))
        if 300 <= status_code <= 599:
            return status_code
    return None


class UpstreamSSEError(RuntimeError):
    """保留流式错误中的上游状态码，便于外层做结构化透传。"""

    def __init__(self, message: str, *, upstream_status: int | None = None):
        super().__init__(message)
        self.upstream_status = upstream_status
        self.retryable = (
            upstream_status in RETRYABLE_STATUS_CODES
            if upstream_status is not None
            else False
        )


def _is_retryable_exception(exc) -> bool:
    """检查异常是否可重试"""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


class _WaitWithRetryAfter(wait_base):
    """等待策略：优先使用 Retry-After 头，否则使用指数退避"""

    def __init__(self, multiplier: float, max_wait: int):
        self._base_wait = wait_random_exponential(multiplier=multiplier, max=max_wait)
        self._protocol_error_base = 3.0

    def __call__(self, retry_state):
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                retry_after = self._parse_retry_after(exc.response)
                if retry_after is not None:
                    return retry_after
            if isinstance(exc, httpx.RemoteProtocolError):
                return self._base_wait(retry_state) + self._protocol_error_base
        return self._base_wait(retry_state)

    def _parse_retry_after(self, response: httpx.Response) -> Optional[float]:
        """解析 Retry-After 头（支持秒数或 HTTP 日期格式）"""
        header = response.headers.get("Retry-After")
        if not header:
            return None
        header = header.strip()

        if header.isdigit():
            return float(header)

        try:
            retry_dt = parsedate_to_datetime(header)
            if retry_dt.tzinfo is None:
                retry_dt = retry_dt.replace(tzinfo=timezone.utc)
            delay = (retry_dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delay)
        except (TypeError, ValueError):
            return None


class GrokSearchProvider(BaseSearchProvider):
    def __init__(self, api_url: str, api_key: str, model: str = "grok-4.1-fast"):
        super().__init__(api_url, api_key)
        self.model = model

    def get_provider_name(self) -> str:
        return "Grok"

    async def search(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None) -> List[SearchResult]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        platform_prompt = ""

        if platform:
            platform_prompt = "\n\nYou should search the web for the information you need, and focus on these platform: " + platform + "\n"

        time_context = get_local_time_info() + "\n"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": search_prompt,
                },
                {"role": "user", "content": time_context + query + platform_prompt},
            ],
            "stream": True,
        }

        await log_info(ctx, f"platform_prompt: { query + platform_prompt}", config.debug_enabled)

        return await self._execute_stream_with_retry(headers, payload, ctx)

    async def search_response(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None) -> SearchResponse:
        raw_content = await self.search(query, platform, min_results, max_results, ctx)
        answer, sources = split_answer_and_sources(raw_content or "")
        return SearchResponse(
            answer=answer,
            sources=sources,
            raw_content=raw_content or "",
            provider=self.get_provider_name(),
            model=self.model,
        )

    async def fetch(self, url: str, ctx=None) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": fetch_prompt,
                },
                {"role": "user", "content": url + "\n获取该网页内容并返回其结构化Markdown格式" },
            ],
            "stream": True,
        }
        return await self._execute_stream_with_retry(headers, payload, ctx)

    async def _parse_streaming_response(self, response, ctx=None) -> str:
        content = ""
        full_body_buffer = [] 
        current_event = "message"
        
        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                continue
            
            full_body_buffer.append(line)

            if line.startswith("event:"):
                current_event = line[6:].strip() or "message"
                continue

            # 兼容 "data: {...}" 和 "data:{...}" 两种 SSE 格式
            if line.startswith("data:"):
                if line in ("data: [DONE]", "data:[DONE]"):
                    break
                try:
                    # 去掉 "data:" 前缀，并去除可能的空格
                    json_str = line[5:].lstrip()
                    data = json.loads(json_str)
                    if current_event == "error" or "error" in data:
                        error = data.get("error") or {}
                        if isinstance(error, dict):
                            message = str(error.get("message") or error).strip()
                            raw_status = error.get("status") or error.get("upstream_status")
                        else:
                            message = str(error).strip()
                            raw_status = None

                        upstream_status = None
                        if isinstance(raw_status, int) and 300 <= raw_status <= 599:
                            upstream_status = raw_status
                        elif isinstance(raw_status, str):
                            upstream_status = _extract_status_from_message(raw_status)
                        if upstream_status is None:
                            upstream_status = _extract_status_from_message(message)

                        raise UpstreamSSEError(
                            message or "Unknown upstream SSE error",
                            upstream_status=upstream_status,
                        )
                    choices = data.get("choices", [])
                    if choices and len(choices) > 0:
                        delta = choices[0].get("delta", {})
                        if "content" in delta:
                            content += delta["content"]
                    current_event = "message"
                except (json.JSONDecodeError, IndexError):
                    continue
                
        if not content and full_body_buffer:
            try:
                full_text = "".join(full_body_buffer)
                data = json.loads(full_text)
                if "choices" in data and len(data["choices"]) > 0:
                    message = data["choices"][0].get("message", {})
                    content = message.get("content", "")
            except json.JSONDecodeError:
                pass
        
        await log_info(ctx, f"content: {content}", config.debug_enabled)

        return content

    async def _execute_stream_with_retry(self, headers: dict, payload: dict, ctx=None) -> str:
        """执行带重试机制的流式 HTTP 请求。

        使用模块级共享 ``AsyncClient`` 复用 TCP/TLS 连接池，避免每次 query
        重新握手；Semaphore 限流仍由 ``maybe_acquire_grok_semaphore`` 控制业务
        并发，与连接池上限解耦。

        **slot 收缩**：把 acquire 放在单次 attempt 内（HTTP 请求时持锁、
        请求结束后立即释放），AsyncRetrying 的退避 sleep 不持锁。这样遇到
        429 + ``Retry-After: 30s`` 等场景时，30 秒等待期间不会绑死并发槽位，
        其他 query 可以正常进入。配合外层 ``hold_grok_semaphore``：外层已持锁
        时内层是 no-op，仍由外层覆盖整个 retry 循环；未持锁的内部辅助调用
        （describe_url / rank_sources 等）则按 attempt 粒度让出 slot。
        """
        client = await get_shared_async_client()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(config.retry_max_attempts + 1),
            wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
            retry=retry_if_exception(_is_retryable_exception),
            reraise=True,
        ):
            with attempt:
                async with maybe_acquire_grok_semaphore():
                    async with client.stream(
                        "POST",
                        f"{self.api_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        response.raise_for_status()
                        return await self._parse_streaming_response(response, ctx)

    async def describe_url(self, url: str, ctx=None) -> dict:
        """让 Grok 阅读单个 URL 并返回 title + extracts"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": url_describe_prompt},
                {"role": "user", "content": url},
            ],
            "stream": True,
        }
        result = await self._execute_stream_with_retry(headers, payload, ctx)
        title, extracts = url, ""
        for line in result.strip().splitlines():
            if line.startswith("Title:"):
                title = line[6:].strip() or url
            elif line.startswith("Extracts:"):
                extracts = line[9:].strip()
        return {"title": title, "extracts": extracts, "url": url}

    async def rank_sources(self, query: str, sources_text: str, total: int, ctx=None) -> list[int]:
        """让 Grok 按查询相关度对信源排序，返回排序后的序号列表"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": rank_sources_prompt},
                {"role": "user", "content": f"Query: {query}\n\n{sources_text}"},
            ],
            "stream": True,
        }
        result = await self._execute_stream_with_retry(headers, payload, ctx)
        order: list[int] = []
        seen: set[int] = set()
        for token in result.strip().split():
            try:
                n = int(token)
                if 1 <= n <= total and n not in seen:
                    seen.add(n)
                    order.append(n)
            except ValueError:
                continue
        # 补齐遗漏的序号
        for i in range(1, total + 1):
            if i not in seen:
                order.append(i)
        return order
