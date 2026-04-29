"""grok_search 包入口。"""

from __future__ import annotations

__all__ = ["mcp"]


def __getattr__(name: str):
    if name == "mcp":
        from .server import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
