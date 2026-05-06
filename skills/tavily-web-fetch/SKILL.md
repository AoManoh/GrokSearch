---
name: tavily-web-fetch
description: Fetch and convert a single URL into clean structured Markdown for reading or downstream processing. Use when the user provides a URL and needs the full page content, or when an earlier search result needs deep extraction. Tavily Extract is primary; Firecrawl Scrape is the automatic fallback with empty-content retry. Requires `TAVILY_API_KEY` and/or `FIRECRAWL_API_KEY` env var — Tavily key at https://app.tavily.com/, Firecrawl key at https://www.firecrawl.dev/.
---

# tavily-web-fetch

Fetch a single URL and convert it to clean structured Markdown via the Tavily Extract API. Falls back to Firecrawl Scrape on empty content / Tavily failure (with an empty-content retry loop).

## When to use

- The user provides a URL and needs the full page content (article, docs page, blog post).
- An earlier `grok-web-search` result references a source that needs deep extraction.
- Skip this skill for URLs that need JavaScript-rendered DOM that Tavily/Firecrawl cannot reach.

## Requirements

- Python 3.10+ with `httpx` (`pip install httpx`).
- **At least one** of these API keys (Tavily is the primary path; Firecrawl is the automatic fallback):
  - Tavily key — sign up at <https://app.tavily.com/>.
  - Firecrawl key — sign up at <https://www.firecrawl.dev/>.

### Configuration

The script reads its config **only** from environment variables (no CLI overrides). Pick whichever style fits your shell:

```bash
# 1. Inline (simplest)
TAVILY_API_KEY=tvly-... python scripts/web_fetch.py --url https://example.com

# 2. Persistent export
export TAVILY_API_KEY=tvly-...
export FIRECRAWL_API_KEY=fc-...   # optional fallback
python scripts/web_fetch.py --url https://example.com

# 3. .env file (no extra Python deps; load via shell)
echo 'TAVILY_API_KEY=tvly-...' >> .env
echo 'FIRECRAWL_API_KEY=fc-...' >> .env
set -a; source .env; set +a
python scripts/web_fetch.py --url https://example.com
```

| Variable | Required | Default |
|---|---|---|
| `TAVILY_API_KEY` | ⚠️ at least one of T/F | — |
| `FIRECRAWL_API_KEY` | ⚠️ at least one of T/F | — |
| `TAVILY_API_URL` | ❌ | `https://api.tavily.com` |
| `FIRECRAWL_API_URL` | ❌ | `https://api.firecrawl.dev/v2` |
| `TAVILY_ENABLED` | ❌ | `true` (`false` skips Tavily entirely) |

## Usage

```bash
python scripts/web_fetch.py --url https://example.com/article \
    [--timeout 60] [--firecrawl-retries 3] [--json]
```

## Output

- Default: the page Markdown is printed to stdout, with a trailing comment line `<!-- fetched via tavily | firecrawl -->` indicating which provider succeeded.
- `--json`: `{"status": "ok", "url": "...", "provider": "tavily" | "firecrawl", "markdown": "..."}`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Config error (no provider key set) |
| 2 | Both providers failed (or returned empty content) |
| 3 | Network/timeout error |

## Notes

The MCP server's secondary fallbacks (Grok-based fetch, basic httpx HTML download) are intentionally **not** included in this skill — to keep the dependency surface obvious. If those are needed, prefer `grok-web-search` or another tool. Hand-maintained copy of `src/grok_search/server.py::_call_tavily_extract` + `_call_firecrawl_scrape`.
