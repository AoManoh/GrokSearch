# GrokSearch 压测矩阵报告

- **started_at_utc**：20260506T161530Z
- **mcp_url**：http://127.0.0.1:18765/mcp
- **batch_timeout**：90s
- **async_tasks**：10
- **async_batch_size**：12
- **total_items**：183
- **latency_p50_ms**：32.28
- **latency_p95_ms**：98587.34
- **latency_max_ms**：98587.34

## 问题归属规则

- `ok`：GrokSearch 结构化返回 `ok/partial`。
- `grok2api_upstream`：GrokSearch 已结构化返回 `upstream_timeout/upstream_network_error/upstream_http_error/upstream_error`，说明请求已进入上游调用路径。
- `groksearch_service`：MCP 工具异常、`internal_error/config_error/invalid_model`、返回结构不一致或服务进程/状态机异常。
- `expected_groksearch_validation`：刻意输入的空 query、非法 task kind、未知 task id 等预期校验行为。
- `chain_or_unknown`：证据不足，不能硬归到任意一侧。

## 归属汇总

| owner | count |
|-------|------:|
| expected_groksearch_validation | 3 |
| grok2api_upstream | 13 |
| ok | 167 |

## 场景汇总

| scenario | ok | upstream | service | expected | unknown |
|----------|---:|---------:|--------:|---------:|--------:|
| async_flood_10x12 | 107 | 13 | 0 | 0 | 0 |
| batch_16 | 16 | 0 | 0 | 0 | 0 |
| batch_32 | 32 | 0 | 0 | 0 | 0 |
| batch_4 | 4 | 0 | 0 | 0 | 0 |
| batch_8 | 8 | 0 | 0 | 0 | 0 |
| expected_invalid_batch_empty | 0 | 0 | 0 | 1 | 0 |
| expected_invalid_task_kind | 0 | 0 | 0 | 1 | 0 |
| expected_unknown_task | 0 | 0 | 0 | 1 | 0 |

## 非正常样本

- `async_flood_10x12` item=0 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=6 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=9 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=10 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=11 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=2 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=5 owner=grok2api_upstream status=error reason=上游错误码 upstream_network_error error={"code": "upstream_network_error", "message": "Grok 上游请求失败: ConnectError", "provider": "grok", "retryable": true}
- `async_flood_10x12` item=1 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=0 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=1 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=7 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=10 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
- `async_flood_10x12` item=9 owner=grok2api_upstream status=error reason=上游错误码 upstream_timeout error={"code": "upstream_timeout", "message": "Grok 上游请求在 90.0s 内未返回，已被 server 切断。", "provider": "grok", "retryable": true, "timeout_seconds": 90.0}
