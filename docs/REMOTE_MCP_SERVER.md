# Remote MCP Server

## Purpose

This document describes how to run `GrokSearch` as a remote HTTP MCP service so that any MCP client can connect with only:

- `base_url`
- `api_key`

The service keeps `GROK_API_URL` and `GROK_API_KEY` on the server side, which means clients do not need to know anything about `grok2api`, token pools, cookies, or Grok web sessions.

## Required Environment Variables

### Upstream runtime

- `GROK_API_URL`
  Example: `http://127.0.0.1:18012/v1`
- `GROK_API_KEY`
  Example: `your-grok2api-api-key`

### MCP service access control

- `MCP_SERVER_API_KEY`
  Example: `replace-with-a-strong-random-key`

## Optional Environment Variables

- `MCP_SERVER_HOST`
  Default: `0.0.0.0`
- `MCP_SERVER_PORT`
  Default: `8765`
- `MCP_SERVER_PATH`
  Default: `/mcp`
- `MCP_SERVER_TRANSPORT`
  Default: `streamable-http`
- `MCP_PUBLIC_BASE_URL`
  Use this when the service is behind a reverse proxy and external clients must see a public hostname.
- `MCP_READY_CHECKS`
  Default: `models`
  Supported values: `models`, `chat`
  When the upstream is `grok2api`, `models` only proves `/v1/models` is reachable. If you need readiness to reflect real chat/search usability, set `MCP_READY_CHECKS=models,chat`.

## Local Startup

```bash
cd /home/oh/projects/grok/GrokSearch
uv sync --extra dev

export GROK_API_URL="http://127.0.0.1:18012/v1"
export GROK_API_KEY="your-grok2api-api-key"
export MCP_SERVER_API_KEY="replace-with-a-strong-random-key"

uv run grok-search-http
```

## Health Endpoints

- `GET /health`
  Liveness probe. Does not hit the upstream.
- `GET /ready`
  Readiness probe. Runs the checks configured by `MCP_READY_CHECKS`.
  - `models`: verifies upstream `/models` access through the configured `GROK_API_URL` and `GROK_API_KEY`
  - `chat`: sends a minimal streaming chat completion to verify that the upstream can actually serve the same class of request used by `web_search`
  - On failures, the response includes structured `checks.*.message` and, when it can be inferred, `checks.*.upstream_status` for cases such as `AppChatReverse: Chat failed, 403`
- `GET /.well-known/mcp-config`
  Returns a ready-to-copy MCP client JSON template plus the service's configured `ready_checks`.

## Client JSON

### Generic transport payload

```json
{
  "type": "streamable-http",
  "url": "https://search.example.com/mcp",
  "headers": {
    "Authorization": "Bearer replace-with-your-mcp-api-key"
  }
}
```

### `mcpServers` wrapper

```json
{
  "grok-search": {
    "type": "streamable-http",
    "url": "https://search.example.com/mcp",
    "headers": {
      "Authorization": "Bearer replace-with-your-mcp-api-key"
    }
  }
}
```

## Export JSON From CLI

```bash
cd /home/oh/projects/grok/GrokSearch
uv run grok-search-http-config --base-url https://search.example.com
```

Default output format is the `mcpServers` wrapper. Use `--format generic` to print only the transport payload.
Use `--format service` to print the same richer payload exposed by `/.well-known/mcp-config`, including `ready_checks`.

## Recommended Production Topology

1. Run `grok2api` with the local Grok account pools.
2. Run `GrokSearch` remote MCP service with `GROK_API_URL` pointing to `grok2api /v1`.
3. Put an HTTPS reverse proxy in front of the MCP service.
4. Configure clients with the public MCP `base_url` and the MCP access key only.
