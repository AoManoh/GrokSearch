---
name: tavily-web-map
description: Map the structure of a website by traversing it like a graph from a root URL, optionally filtered by natural-language instructions, and produce a list/tree of discovered URLs. Use when the user wants to explore the surface area of a docs site, find all pages under a section, or get a sitemap before deciding which pages to fetch. Requires `TAVILY_API_KEY` env var — get one at https://app.tavily.com/.
---

# tavily-web-map

Map the structure of a website by traversing it like a graph from a root URL via the Tavily Map API. Returns a list/tree of discovered URLs.

## When to use

- The user wants a sitemap or wants to find all pages under a section of a docs site.
- You need to discover URLs *before* deciding which pages to fetch in detail (use `tavily-web-fetch` for the actual extraction).
- The user provides natural-language filtering hints (e.g. "only API reference pages") that should narrow the crawl.

## Requirements

- Python 3.10+ with `httpx` (`pip install httpx`).
- A Tavily API key — sign up at <https://app.tavily.com/> and copy a key from the dashboard.

### Configuration

The script reads its config **only** from environment variables (no CLI overrides). Pick whichever style fits your shell:

```bash
# 1. Inline (simplest)
TAVILY_API_KEY=tvly-... python scripts/web_map.py --url https://docs.example.com

# 2. Persistent export (current shell only)
export TAVILY_API_KEY=tvly-...
python scripts/web_map.py --url https://docs.example.com

# 3. .env file (no extra Python deps; load via shell)
echo 'TAVILY_API_KEY=tvly-...' >> .env
set -a; source .env; set +a
python scripts/web_map.py --url https://docs.example.com
```

| Variable | Required | Default |
|---|---|---|
| `TAVILY_API_KEY` | ✅ | — |
| `TAVILY_API_URL` | ❌ | `https://api.tavily.com` |

## Usage

```bash
python scripts/web_map.py --url https://docs.example.com \
    [--instructions "..."] \
    [--max-depth 1..5] [--max-breadth 1..500] \
    [--limit 1..500] [--timeout 10..150] \
    [--json]
```

Out-of-range values are clamped automatically with a stderr warning.

## Output

- Default: Markdown with a header, indented URL tree grouped by path, and a `total: N urls | source: tavily` footer.
- `--json`: `{"status": "ok", "root": "<url>", "urls": [...], "count": N}`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Config error (e.g. missing `TAVILY_API_KEY`) |
| 2 | Tavily upstream error (non-2xx after retries) |
| 3 | Network/timeout error |

## Notes

The script retries on `408 / 429 / 500 / 502 / 503 / 504` (max 3 retries, exponential backoff, honours `Retry-After` for 429). Hand-maintained copy of the relevant logic from `src/grok_search/server.py::_call_tavily_map`.
