---
name: grok-web-search
description: Run an AI-powered web search using Grok and return an answer with cited sources. Use when the user asks about current events, recent technical changes, library docs that may be outdated in training data, or any factual question that requires fresh web evidence. Optionally augment with Tavily / Firecrawl extra sources via --extra-sources N. Requires `GROK_API_URL` and `GROK_API_KEY` env vars — get a key at https://console.x.ai/ or any OpenAI-compatible Grok proxy.
---

# grok-web-search

Run an AI-powered web search via Grok (OpenAI-compatible `/chat/completions` or xai `/responses`) and return an answer + cited sources. Optionally augment with Tavily / Firecrawl Search supplemental sources.

## When to use

- Current events, recent technical changes, library docs that may be outdated in training data, or any factual question that requires fresh web evidence.
- The user wants a single concise answer + a list of sources, not full page extraction (use `tavily-web-fetch` for full-page reads).
- Multi-step research benefits from running `search-planning` first to break the question into mutually-exclusive sub-queries.

## Requirements

- Python 3.10+ with `httpx` (`pip install httpx`).
- A Grok-compatible endpoint and API key — get one from <https://console.x.ai/> (official) or any OpenAI-compatible Grok proxy you operate.

### Configuration

The script reads its config **only** from environment variables. CLI flags exist only for query/runtime knobs (`--model`, `--platform`, `--extra-sources`, `--timeout`, `--json`); credentials never come from the command line. Pick whichever style fits your shell:

```bash
# 1. Inline (simplest)
GROK_API_URL=https://api.x.ai/v1 GROK_API_KEY=xai-... \
    python scripts/web_search.py --query "what's new in React 19?"

# 2. Persistent export
export GROK_API_URL=https://api.x.ai/v1
export GROK_API_KEY=xai-...
export GROK_MODEL=grok-4.20-fast     # optional; omit to auto-select from /models
python scripts/web_search.py --query "what's new in React 19?"

# 3. .env file (no extra Python deps; load via shell)
cat >> .env <<'EOF'
GROK_API_URL=https://api.x.ai/v1
GROK_API_KEY=xai-...
GROK_MODEL=grok-4.20-fast
EOF
set -a; source .env; set +a
python scripts/web_search.py --query "what's new in React 19?"
```

| Variable | Required | Default |
|---|---|---|
| `GROK_API_URL` | ✅ | — |
| `GROK_API_KEY` | ✅ | — |
| `GROK_MODEL` | ❌ | auto-selected from `/models`; fallback `grok-4.1-fast` (overridable via `--model`) |
| `GROK_SEARCH_PROVIDER` | ❌ | `auto` (∈ {`auto`,`chat`,`responses`}; `auto` → `/responses` for `api.x.ai`, else `/chat/completions`) |
| `TAVILY_API_KEY` | ❌ | only consulted when `--extra-sources > 0` |
| `FIRECRAWL_API_KEY` | ❌ | only consulted when `--extra-sources > 0` |

Standard proxy vars (`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`) are honoured by `httpx`.
If `ALL_PROXY` / `all_proxy` uses invalid `socks://...` syntax while `HTTP_PROXY` or `HTTPS_PROXY` is available, the script ignores only that SOCKS fallback so `httpx` can still use the HTTP proxy. If SOCKS is the only configured proxy, use `socks5://...` with `pip install httpx[socks]`, switch to an `http://` proxy, or clear the variable.

## Usage

```bash
python scripts/web_search.py --query "what's new in React 19?" \
    [--platform "GitHub, Reddit"] \
    [--model grok-4.20-fast] \
    [--extra-sources 5] \
    [--timeout 120] \
    [--json]

python scripts/web_search.py --list-models [--json]
```

When the query contains time-sensitive keywords (CN: `最新/今天/...`; EN: `latest/today/...`) the script automatically prepends a `[Current Time Context]` block to the user message.

## Output

Default Markdown:

```
## Answer

<answer>

## Sources

1. [<title>](<url>) — <description>
…

---
provider: chat | responses
model: <effective-model-id>
```

`--json`:

```json
{
  "status": "ok",
  "provider": "chat" | "responses",
  "model": "...",
  "answer": "...",
  "sources": [{"url": "...", "title": "...", "description": "...", "provider": "grok|tavily|firecrawl"}]
}
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Config error (missing `GROK_API_URL` / `GROK_API_KEY`) |
| 2 | Upstream error (Grok HTTP non-2xx, SSE error frame, retries exhausted) |
| 3 | Network/timeout error |

## Troubleshooting

- `Unknown scheme for proxy URL 'socks://...'`: use the updated script. It drops invalid `ALL_PROXY` / `all_proxy` fallbacks when an HTTP proxy is already configured. If SOCKS is the only proxy, configure `socks5://...` and install `httpx[socks]`.
- `model_not_found` / `400 Bad Request`: run `python scripts/web_search.py --list-models` and pass one of the returned IDs via `--model` or `GROK_MODEL`. The script includes the upstream response body in errors to make this diagnosis visible.

## Notes

- Extra-source calls (Tavily/Firecrawl Search) are best-effort. Failures are silent; the merged source list is simply shorter and the exit code reflects only the Grok call.
- Quota split when both keys are present mirrors MCP exactly: Firecrawl gets all of `--extra-sources`, Tavily gets 0. Set only one of the two keys to use Tavily for supplements.
- Hand-maintained copy of the relevant logic from `src/grok_search/providers/grok.py` + `providers/responses.py` + `sources.py` + `server.py`.
