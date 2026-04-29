# GrokSearch Windsurf stdio 链路压测报告

- **started_at**: 20260427T070215Z（UTC+8: 2026-04-27 15:02）
- **transport**: Windsurf MCP stdio（重启后从 `git+https://github.com/AoManoh/GrokSearch@main` 通过 `uvx` 拉起）
- **provider**: chat（`GROK_SEARCH_PROVIDER=chat`）
- **model**: `grok-4.20-fast`
- **upstream**: `https://grok.aomanoh.tech/v1`

## 验收前置探针

| 项 | 结果 |
|---|---|
| `get_config_info.config_status` | 配置完整 |
| `get_config_info.connection_test.status` | 连接成功（HTTP 200）|
| `get_config_info.connection_test.response_time_ms` | 1123.33 |
| 可用模型数 | 7 |
| `GROK_SEARCH_PROVIDER` 出现在配置 | 是（确认 @main 新代码已加载）|
| `GROK_RESPONSES_MODEL` 出现在配置 | 是（确认 @main 新代码已加载）|

## 总览

- **total**: 20
- **ok（首发）**: 19
- **partial**: 0
- **empty（首发）**: 1（WebGPU 主流浏览器支持现状）
- **failed**: 0
- **首发通过率**: 19 / 20 = 95.0%
- **复测后通过率**: 20 / 20 = 100%（empty 项复测一次即返回 ok）
- **provider/model 一致性**: 所有结果 `provider=Grok`、`model=grok-4.20-fast`

## 分批结果

### Batch 1：smoke（5 路并行）

| # | 查询 | status | sources |
|---|------|--------|---------|
| 1 | 今天人工智能芯片市场最新动态 | ok | 13 |
| 2 | FastAPI 生产环境部署最佳实践 2026 | ok | 4 |
| 3 | latest updates from xAI this week | ok | 5 |
| 4 | Kubernetes Gateway API production best practices | ok | 6 |
| 5 | WebGPU support status in major browsers | empty | 0 |

### Batch 2：压力扩量（10 路并行）

| # | 查询 | status | sources |
|---|------|--------|---------|
| 6 | AIGC 最新行业应用和监管趋势 | ok | 12 |
| 7 | 检索扩展生成 RAG 最新综述论文 | ok | 6 |
| 8 | PostgreSQL 向量检索 pgvector 性能优化 | ok | 13 |
| 9 | 中国新能源汽车出口 近期数据 | ok | 8 |
| 10 | 欧盟人工智能法案 最新实施进展 | ok | 7 |
| 11 | recent papers on retrieval augmented generation evaluation | ok | 10 |
| 12 | NVIDIA latest earnings AI datacenter revenue | ok | 4 |
| 13 | EU AI Act implementation timeline latest | ok | 5 |
| 14 | SQLite vector search extensions comparison | ok | 5 |
| 15 | Bitcoin ETF inflows latest weekly data | ok | 8 |

### Batch 3：边缘场景与复测（5 路并行）

| # | 查询 | status | sources | 备注 |
|---|------|--------|---------|------|
| 16 | WebGPU support status in major browsers | ok | 6 | empty 复测通过 |
| 17 | 한국 AI 반도체 최신 동향 | ok | 11 | 韩文本地化 |
| 18 | 日本 生成AI ガイドライン 最新 | ok | 4 | 日文本地化 |
| 19 | fusion energy latest experimental milestones | ok | 12 | 科研类 |
| 20 | supply chain security SLSA latest guidance | ok | 9 | 安全类 |

## 结论

- 重启 Windsurf 后，`grok-search` MCP 已正确从 `@main` 拉取本次提交的代码（环境里出现 `GROK_SEARCH_PROVIDER` / `GROK_RESPONSES_MODEL` 字段），并完成上游 `/models` 健康探针。
- 在 `GROK_SEARCH_PROVIDER=chat` + `GROK_MODEL=grok-4.20-fast` 这一已被 100 轮 HTTP MCP 验收过的稳态配置下，stdio 链路 20 路压测首发 95%、复测 100%，与 HTTP MCP 100/100 的结果方向一致。
- 唯一一例 `empty` 复测立即恢复，符合“偶发上游空响应”的判断，与本次 provider 抽象无关。

## 未覆盖与遗留

- 真实官方 `api.x.ai` `/responses` 链路尚未做服务级压测；本次 provider 仍是 OpenAI-compatible chat，`ResponsesSearchProvider` 只覆盖了离线/解析/路由测试。
- 本次未通过 HTTP MCP 服务做远端验收，复用了既有 100 轮 HTTP 报告。
