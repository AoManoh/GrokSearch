# GrokSearch 压测矩阵报告

- **started_at_utc**：20260506T151803Z
- **mcp_url**：http://127.0.0.1:18765/mcp
- **batch_timeout**：60s
- **async_tasks**：6
- **async_batch_size**：8
- **total_items**：111
- **latency_p50_ms**：54287.93
- **latency_p95_ms**：139803.37
- **latency_max_ms**：139803.37

## 补充验证

- 顺序基线：`groksearch-acceptance-20260506T151138Z.jsonl` 中断前完成 11 轮，11/11 `ok`，无失败；因顺序 100 轮预计耗时超过半小时，已切换到更高压力的批量/异步矩阵。
- 本地回归：`uv run --extra dev pytest tests/test_web_search_batch.py tests/test_task_store.py tests/test_http_service.py tests/test_server_runtime.py`，44 passed，1 个 `.pytest_cache` 权限 warning。
- 显式短超时：`web_search(timeout="3s")` 在 3579.06ms 返回 `error.code=upstream_timeout`、`retryable=true`，属于刻意短超时的预期结构化切断，不计为缺陷。
- 取消路径：提交 8 query 异步 batch 后立即 `cancel_search_task`，任务进入 `cancelled`，`cancel_hint=stress_cancel`。
- 残留任务：`list_search_tasks(states=["queued","running"])` 返回 `count=0`。

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
| ok | 108 |

## 场景汇总

| scenario | ok | upstream | service | expected | unknown |
|----------|---:|---------:|--------:|---------:|--------:|
| async_flood_6x8 | 48 | 0 | 0 | 0 | 0 |
| batch_16 | 16 | 0 | 0 | 0 | 0 |
| batch_32 | 32 | 0 | 0 | 0 | 0 |
| batch_4 | 4 | 0 | 0 | 0 | 0 |
| batch_8 | 8 | 0 | 0 | 0 | 0 |
| expected_invalid_batch_empty | 0 | 0 | 0 | 1 | 0 |
| expected_invalid_task_kind | 0 | 0 | 0 | 1 | 0 |
| expected_unknown_task | 0 | 0 | 0 | 1 | 0 |

## 非正常样本

- 无
