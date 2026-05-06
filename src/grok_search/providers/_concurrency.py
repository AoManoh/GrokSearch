"""上游 grok2api 调用的进程级并发限流。

通过共享 Semaphore 保证：

- 单个 web_search 调用不会被无限放大
- 批量入口（web_search_batch）以及客户端并行调用 web_search 都共享同一上限
- responses 与 chat 两条 provider 路径共享同一限流，避免双倍打满上游

并发上限来源于 ``Config.grok_concurrency``（环境变量 ``GROK_CONCURRENCY``，
默认 4，范围 1-32）。修改环境变量后需要调用 ``reset_grok_semaphore`` 才会
生效；正常运行时只在进程启动时读取一次。

外层在调用 ``asyncio.wait_for`` 之前，应通过 ``hold_grok_semaphore`` 显式获取
slot，避免排队等位的时间被算入超时（这是 2026-05-06 stress test 暴露的
P0 缺陷）。在外层已持有 slot 的上下文里，provider 内部用
``maybe_acquire_grok_semaphore`` 自动复用，不会重复 acquire 形成死锁。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars

from ..config import config


_SEMAPHORE: asyncio.Semaphore | None = None
_SEMAPHORE_VALUE: int | None = None

# 标记当前 task 是否已经持有 grok 上游 Semaphore slot。
# 通过 contextvars 传播到 asyncio.create_task 派生的子任务，
# 让 provider 内部的 maybe_acquire_grok_semaphore 可以判断是否要再 acquire。
_SEM_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "grok_sem_held", default=False
)


def get_grok_semaphore() -> asyncio.Semaphore:
    """返回进程级 grok 上游请求 Semaphore，按需懒加载。"""
    global _SEMAPHORE, _SEMAPHORE_VALUE
    if _SEMAPHORE is None:
        value = max(1, int(config.grok_concurrency))
        _SEMAPHORE = asyncio.Semaphore(value)
        _SEMAPHORE_VALUE = value
    return _SEMAPHORE


def get_grok_semaphore_value() -> int:
    """返回当前 Semaphore 容量（用于诊断输出）。"""
    if _SEMAPHORE_VALUE is None:
        return int(config.grok_concurrency)
    return _SEMAPHORE_VALUE


def reset_grok_semaphore() -> None:
    """重置 Semaphore 单例，仅供测试或显式重新加载并发参数使用。"""
    global _SEMAPHORE, _SEMAPHORE_VALUE
    _SEMAPHORE = None
    _SEMAPHORE_VALUE = None


def is_grok_semaphore_held() -> bool:
    """诊断用：当前 task 是否在 hold_grok_semaphore 上下文内。"""
    return _SEM_HELD.get()


@contextlib.asynccontextmanager
async def hold_grok_semaphore():
    """在外层显式持有 Grok 上游 Semaphore slot。

    用法：调用方应该把这个 ``async with`` 放在 ``asyncio.wait_for`` **外面**，
    这样排队等位的时间不会被算进超时；只有真正拿到 slot 之后的上游请求时间
    才会受到 wait_for 的约束。

    示例::

        async with hold_grok_semaphore():
            try:
                result = await asyncio.wait_for(
                    provider.search(query, platform),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                ...
    """
    sem = get_grok_semaphore()
    async with sem:
        token = _SEM_HELD.set(True)
        try:
            yield
        finally:
            _SEM_HELD.reset(token)


@contextlib.asynccontextmanager
async def maybe_acquire_grok_semaphore():
    """provider 内部使用：若外层已 hold_grok_semaphore 则复用，否则自行 acquire。

    这样 provider 既能被外层超时安全地包裹（外层走 hold_grok_semaphore），
    也能在没有外层包裹时（例如 describe_url / rank_sources 这些内部辅助调用）
    保持原有的限流语义。
    """
    if _SEM_HELD.get():
        # 外层已持有同一 task 的 slot，避免 asyncio.Semaphore 不可重入导致死锁
        yield
        return
    sem = get_grok_semaphore()
    async with sem:
        token = _SEM_HELD.set(True)
        try:
            yield
        finally:
            _SEM_HELD.reset(token)


__all__ = [
    "get_grok_semaphore",
    "get_grok_semaphore_value",
    "reset_grok_semaphore",
    "hold_grok_semaphore",
    "maybe_acquire_grok_semaphore",
    "is_grok_semaphore_held",
]
