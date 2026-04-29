# GrokSearch 100 轮服务验收报告

- **started_at**：20260426T141338Z
- **total**：3
- **ok**：0
- **partial**：0
- **failed**：3
- **providers**：unknown
- **latency_p50_ms**：964.29
- **latency_max_ms**：1629.56

## 分类结果

| category | total | ok | partial | failed |
|----------|-------|----|---------|--------|
| zh_fresh | 1 | 0 | 0 | 1 |
| zh_regression | 2 | 0 | 0 | 2 |

## 失败样本

- **round 1**：AIGC 最新行业应用和监管趋势 status=error error={'code': 'upstream_error', 'message': 'Grok 上游流式错误: Chat upstream returned 403', 'provider': 'grok', 'retryable': False}
- **round 2**：论文降重 合规方法和学术诚信要求 status=error error={'code': 'upstream_error', 'message': 'Grok 上游流式错误: Chat upstream returned 403', 'provider': 'grok', 'retryable': False}
- **round 3**：今天人工智能芯片市场最新动态 status=error error={'code': 'upstream_error', 'message': 'Grok 上游流式错误: Chat upstream returned 403', 'provider': 'grok', 'retryable': False}
