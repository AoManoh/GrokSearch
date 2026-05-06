#!/usr/bin/env python3
"""tavily-web-fetch: fetch a single URL as Markdown.

Tavily Extract is the primary path; Firecrawl Scrape is the fallback (with
empty-content retry). Independent skill — no dependency on the GrokSearch
package or MCP runtime. Reads only environment variables.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx


# ---- Tavily Extract -------------------------------------------------------

async def call_tavily_extract(url: str) -> str | None:
    if os.environ.get("TAVILY_ENABLED", "true").lower() == "false":
        return None
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return None
    api_url = os.environ.get("TAVILY_API_URL", "https://api.tavily.com").rstrip("/")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {"urls": [url], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{api_url}/extract", headers=headers, json=body)
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
    except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError):
        return None

    results = data.get("results") or []
    for item in results:
        if isinstance(item, dict):
            content = (item.get("raw_content") or item.get("content") or "").strip()
            if content:
                return content
    return None


# ---- Firecrawl Scrape -----------------------------------------------------

async def call_firecrawl_scrape(url: str, retries: int = 3) -> str | None:
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        return None
    api_url = os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v2").rstrip("/")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {"url": url, "formats": ["markdown"]}

    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(f"{api_url}/scrape", headers=headers, json=body)
                if resp.status_code != 200:
                    return None
                data = resp.json() or {}
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError):
            return None
        markdown = ((data.get("data") or {}).get("markdown") or "").strip()
        if markdown:
            return markdown
        # else fall through to retry
    return None


# ---- Orchestration --------------------------------------------------------

async def fetch(url: str, *, firecrawl_retries: int) -> tuple[str | None, str | None]:
    """Try Tavily Extract first; on miss, try Firecrawl Scrape.

    Returns (markdown, provider) where provider is 'tavily' / 'firecrawl' /
    None on total failure.
    """
    md = await call_tavily_extract(url)
    if md:
        return md, "tavily"
    md = await call_firecrawl_scrape(url, retries=firecrawl_retries)
    if md:
        return md, "firecrawl"
    return None, None


# ---- CLI ------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web_fetch.py",
        description="Fetch a URL as Markdown via Tavily Extract (Firecrawl fallback).",
    )
    p.add_argument("--url", required=True, help="Target URL to fetch.")
    p.add_argument("--timeout", type=int, default=60, help="Per-request timeout (seconds).")
    p.add_argument("--firecrawl-retries", type=int, default=3, help="Empty-content retry count for Firecrawl.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of plain Markdown.")
    return p


_MISSING_KEY_HINT = (
    "neither TAVILY_API_KEY nor FIRECRAWL_API_KEY is set. Export at least "
    "one (`export TAVILY_API_KEY=...` / `export FIRECRAWL_API_KEY=...`), or "
    "load a .env file via `set -a; source .env; set +a`. Sign up: "
    "https://app.tavily.com/  or  https://www.firecrawl.dev/."
)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    has_tavily = bool(os.environ.get("TAVILY_API_KEY", "").strip())
    has_firecrawl = bool(os.environ.get("FIRECRAWL_API_KEY", "").strip())
    if not (has_tavily or has_firecrawl):
        if args.json:
            print(json.dumps({"status": "error", "error": {"code": "config_error", "message": _MISSING_KEY_HINT}}))
        else:
            print(f"error: {_MISSING_KEY_HINT}", file=sys.stderr)
        return 1

    try:
        md, provider = asyncio.run(fetch(args.url, firecrawl_retries=args.firecrawl_retries))
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        msg = str(exc) or exc.__class__.__name__
        if args.json:
            print(json.dumps({"status": "error", "error": {"code": "network_error", "message": msg}}))
        else:
            print(f"error: network failure: {msg}", file=sys.stderr)
        return 3

    if md is None or provider is None:
        msg = "all providers failed (Tavily empty/error and Firecrawl empty/error)"
        if args.json:
            print(json.dumps({"status": "error", "error": {"code": "upstream_error", "message": msg}}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "status": "ok",
            "url": args.url,
            "provider": provider,
            "markdown": md,
        }, ensure_ascii=False))
    else:
        sys.stdout.write(md)
        if not md.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.write(f"\n<!-- fetched via {provider} -->\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
