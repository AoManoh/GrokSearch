#!/usr/bin/env python3
"""tavily-web-map: traverse a website via Tavily Map API.

Independent skill — no dependency on the GrokSearch package or MCP runtime.
Reads only environment variables. See ../SKILL.md for usage.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urlparse

import httpx


# ---- Helpers --------------------------------------------------------------

def clamp(value: int, lo: int, hi: int) -> int:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def render_tree(urls: list[str], *, root: str) -> str:
    """Group URLs by path prefix into an indented Markdown tree."""
    # Normalise: keep order of first appearance, dedupe.
    seen: set[str] = set()
    ordered: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    lines = [f"# Site Map: {root}", ""]
    if not ordered:
        lines.append("_(no URLs discovered)_")
    else:
        # Build a path-segment tree.
        tree: dict = {}
        for u in ordered:
            parsed = urlparse(u)
            segments = [s for s in parsed.path.split("/") if s]
            node = tree.setdefault(parsed.netloc, {"_url": None, "_children": {}})
            if not segments:
                node["_url"] = u
                continue
            cursor = node["_children"]
            for i, seg in enumerate(segments):
                cursor.setdefault(seg, {"_url": None, "_children": {}})
                if i == len(segments) - 1:
                    cursor[seg]["_url"] = u
                cursor = cursor[seg]["_children"]

        def emit(node: dict, depth: int) -> None:
            if node.get("_url"):
                lines.append("  " * depth + f"- {node['_url']}")
                child_depth = depth + 1
            else:
                child_depth = depth
            for _, sub in sorted(node.get("_children", {}).items()):
                emit(sub, child_depth)

        for _, host_node in sorted(tree.items()):
            emit(host_node, 0)

    lines.append("")
    lines.append("---")
    lines.append(f"total: {len(ordered)} urls | source: tavily")
    return "\n".join(lines)


# ---- Retry policy ---------------------------------------------------------

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, *, headers: dict, json_body: dict,
    max_attempts: int = 4,
) -> httpx.Response:
    """POST with exponential backoff on retryable status / network errors.

    `max_attempts` counts the initial call plus retries (so 4 = 1+3).
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = await client.post(url, headers=headers, json=json_body)
            if resp.status_code in RETRYABLE_STATUS and attempt < max_attempts - 1:
                wait = _retry_wait(resp, attempt)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt >= max_attempts - 1:
                raise
            await asyncio.sleep(min(2 ** attempt, 10))
    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop exited unexpectedly")


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    """Honour Retry-After (numeric or HTTP-date) when present, else exp-backoff."""
    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After")
        if ra:
            ra = ra.strip()
            if ra.isdigit():
                return float(ra)
            try:
                from email.utils import parsedate_to_datetime
                from datetime import datetime, timezone
                dt = parsedate_to_datetime(ra)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                pass
    return min(2 ** attempt, 10)


# ---- Tavily Map call ------------------------------------------------------

async def call_tavily_map(
    *, url: str, instructions: str, max_depth: int, max_breadth: int,
    limit: int, timeout: int,
) -> list[str]:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    api_url = os.environ.get("TAVILY_API_URL", "https://api.tavily.com").rstrip("/")

    body: dict = {
        "url": url,
        "max_depth": max_depth,
        "max_breadth": max_breadth,
        "limit": limit,
    }
    if instructions:
        body["instructions"] = instructions

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await _post_with_retry(client, f"{api_url}/map", headers=headers, json_body=body)
        data = resp.json() or {}
    results = data.get("results") or []
    out: list[str] = []
    for item in results:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            out.append(item["url"])
        elif isinstance(item, str):
            out.append(item)
    return out


# ---- CLI ------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web_map.py",
        description="Map a website's structure via Tavily Map API.",
    )
    p.add_argument("--url", required=True, help="Root URL to start mapping from.")
    p.add_argument("--instructions", default="", help="Natural-language filter for the crawler.")
    p.add_argument("--max-depth", type=int, default=1, help="Max traversal depth (1-5).")
    p.add_argument("--max-breadth", type=int, default=20, help="Max links per page (1-500).")
    p.add_argument("--limit", type=int, default=50, help="Total link cap (1-500).")
    p.add_argument("--timeout", type=int, default=150, help="Per-request timeout in seconds (10-150).")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    return p


_MISSING_KEY_HINT = (
    "TAVILY_API_KEY is not set. Export it (`export TAVILY_API_KEY=...`) "
    "or load a .env file via `set -a; source .env; set +a` before invoking. "
    "Sign up at https://app.tavily.com/ to get a key."
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Clamp ranges with stderr warnings (matches MCP behaviour).
    bounds = {
        "max_depth": (args.max_depth, 1, 5),
        "max_breadth": (args.max_breadth, 1, 500),
        "limit": (args.limit, 1, 500),
        "timeout": (args.timeout, 10, 150),
    }
    clamped: dict[str, int] = {}
    for name, (val, lo, hi) in bounds.items():
        new = clamp(val, lo, hi)
        if new != val:
            print(f"warning: --{name.replace('_', '-')}={val} clamped to {new}", file=sys.stderr)
        clamped[name] = new

    try:
        urls = asyncio.run(call_tavily_map(
            url=args.url,
            instructions=args.instructions,
            max_depth=clamped["max_depth"],
            max_breadth=clamped["max_breadth"],
            limit=clamped["limit"],
            timeout=clamped["timeout"],
        ))
    except RuntimeError as exc:
        # Configuration errors (missing TAVILY_API_KEY).
        msg = str(exc)
        if "TAVILY_API_KEY" in msg:
            msg = _MISSING_KEY_HINT
        if args.json:
            print(json.dumps({
                "status": "error",
                "error": {"code": "config_error", "message": msg},
            }))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        msg = str(exc) or exc.__class__.__name__
        if args.json:
            print(json.dumps({
                "status": "error",
                "error": {"code": "network_error", "message": msg},
            }))
        else:
            print(f"error: network failure: {msg}", file=sys.stderr)
        return 3
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        msg = str(exc) or exc.__class__.__name__
        if args.json:
            print(json.dumps({
                "status": "error",
                "error": {"code": "upstream_error", "message": msg, "upstream_status": status},
            }))
        else:
            print(f"error: tavily map failed: {msg}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) or exc.__class__.__name__
        if args.json:
            print(json.dumps({
                "status": "error",
                "error": {"code": "upstream_error", "message": msg},
            }))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "status": "ok",
            "root": args.url,
            "urls": urls,
            "count": len(urls),
        }, ensure_ascii=False))
    else:
        print(render_tree(urls, root=args.url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
