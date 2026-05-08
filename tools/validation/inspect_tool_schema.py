"""单次调用 list_tools 拿 web_search_batch 的 inputSchema，看 auto_async_threshold 是否被注册。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def main() -> int:
    env = os.environ.copy()
    env.setdefault("GROK_API_URL", "https://grok.aomanoh.tech/v1")
    env.setdefault("GROK_API_KEY", "sk-aomanoh")
    env.setdefault("GROK_MODEL", "grok-4.20-fast")
    env.setdefault("GROK_SEARCH_PROVIDER", "chat")
    env["PYTHONUNBUFFERED"] = "1"

    transport = StdioTransport(
        command="uvx",
        args=["--refresh", "--from", str(PROJECT_ROOT), "grok-search"],
        env=env,
        keep_alive=False,
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        for tool in tools:
            if tool.name == "web_search_batch":
                schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
                print("[web_search_batch.inputSchema]")
                print(json.dumps(schema, ensure_ascii=False, indent=2, default=str))
                return 0
    print("[ERROR] web_search_batch tool not found")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
