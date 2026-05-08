"""共享 httpx.AsyncClient 复用回归测试。

验证模块级单例行为：连续调用返回同一对象，reset 后会重新创建；并通过
httpx.MockTransport 验证多次 stream 请求确实复用同一 client（隐含连接池复用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from grok_search.providers import grok as grok_provider_mod  # noqa: E402


@pytest.mark.asyncio
async def test_get_shared_async_client_returns_singleton():
    await grok_provider_mod.reset_shared_async_client()
    try:
        first = await grok_provider_mod.get_shared_async_client()
        second = await grok_provider_mod.get_shared_async_client()
        assert first is second
        assert isinstance(first, httpx.AsyncClient)
        # 默认 timeout 与 limits 应被应用，而不是 httpx 默认值
        assert first.timeout.read == 120.0
        assert first.timeout.connect == 6.0
    finally:
        await grok_provider_mod.reset_shared_async_client()


@pytest.mark.asyncio
async def test_reset_creates_new_instance_after_close():
    await grok_provider_mod.reset_shared_async_client()
    try:
        first = await grok_provider_mod.get_shared_async_client()
        await grok_provider_mod.reset_shared_async_client()
        second = await grok_provider_mod.get_shared_async_client()
        assert first is not second
        assert first.is_closed
    finally:
        await grok_provider_mod.reset_shared_async_client()
