# GrokSearch MCP 高并发压力测试与缺陷分析

- **执行时间**：2026-05-06 18:04~18:08 (UTC+8) / 10:04~10:08Z
- **被测版本**：本地工作树 `c:\projects\grok\GrokSearch`，HEAD = `feb6ece`，含未提交改动
  - 新增 `task_store.py`、`providers/_concurrency.py`、`tests/test_task_store.py`、`tests/test_web_search_batch.py`
  - `server.py` +386 行（新增 4 个异步任务工具 + 共享 Semaphore + 可调 timeout）
- **后端**：`https://grok.aomanoh.tech/v1`，模型 `grok-4.20-fast`
- **客户端**：Windsurf MCP stdio，`uvx --refresh --from C:/projects/grok/GrokSearch grok-search`
- **关键 server 配置**：`GROK_CONCURRENCY=4`、`GROK_REQUEST_TIMEOUT=120s`、`GROK_SEARCH_PROVIDER=chat`
- **总请求量**：6 个异步 task，覆盖 33 个 query（其中 2 个空字符串被静默丢弃，实际 31 个）+ 多次同步 `web_search` / `web_search_batch` 验证

## 1. 测试矩阵与结果

### 1.1 异步任务 (`submit_search_task` + `get_search_task_result`)

| Task ID | Kind | Queries | Timeout | 提交时刻 (UTC) | ok / err | 备注 |
|---|---|---:|---:|---|---|---|
| `task-8d9fe952f09d4639` | batch | 8 中文科技资讯 | 60s | 10:04:55.588 | **7 / 1** | 最早提交，几乎全通 |
| `task-cc3aaa78e7a24e6e` | batch | 8 英文 AI 巨头 | 60s | 10:04:55.961 | **3 / 5** | 失败率 62.5% |
| `task-18e897a5beaf45ab` | batch | 8 中文羊毛福利 | 60s | 10:04:55.972 | **0 / 8** | **整批 60s 整时间整齐失败** |
| `task-d520f1b191b54c57` | batch | 8 英文加密货币 | 60s | 10:04:55.976 | **0 / 8** | **整批 60s 整时间整齐失败** |
| `task-2329780fa659402e` | search | HN 单条 | 3s | 10:04:55.985 | err | 故意短超时验证 |
| `task-ae85f959770b4704` | batch | `["", " ", "AAA", "BBB"]` | 30s | 10:04:55.986 | 0 / 2 | 空字符串被丢，AAA/BBB 30s 全超时 |

合计 31 个有效 query：**14 ok / 17 error**，整体失败率 **54.8%**。

### 1.2 同步对照实验

| 用例 | 行为 |
|---|---|
| 单次 `web_search` | ok，正常返回 |
| 4-query `web_search_batch` (= concurrency 上限) | **4 / 4 ok** |
| 第二轮 8-query 中文 batch（独立提交，无并发争用） | 8 / 8 ok |

并发量 ≤ Semaphore 容量时，工作完美。压力下的失败具有强方向性。

## 2. 暴露的缺陷与根因分析

### 2.1 [P0 严重] timeout 计时包含等 Semaphore 排队，错误信息严重误导

**现象**：在 6 个 task 几乎同时提交后，按提交顺序失败率单调递增——
第 1 批 12.5% → 第 2 批 62.5% → 第 3、4 批 **100%**。第 3、4 批所有 16 个 query 全部在
`finished_at = submitted_at + 60.04s` 时刻整齐失败，错误信息均为：

```text
Grok 上游请求在 60.0s 内未返回，已被 server 切断。
error.code = upstream_timeout
```

但实际上这些 query **从未真正发送给上游**——它们一直卡在 Semaphore acquire。
单次 grok 上游 query 实测耗时 ~10–60s，concurrency=4 时 32 query 至少需要
4–8 轮 ≈ 80–300s 才能全部发出，所以最后入队的 query 永远等不到 60s 内开始。

**根因（`providers/grok.py:287-292` × `server.py:574-595`）**：

```python
# server.py
async def _safe_grok():
    try:
        return await asyncio.wait_for(
            search_response(query, platform),   # 内部要 acquire sem 才会发 HTTP
            timeout=timeout_seconds,             # ← wait_for 包住排队 + acquire + HTTP + 重试
        ), None
```

```python
# providers/grok.py
async def _execute_stream_with_retry(...):
    sem = get_grok_semaphore()
    async with sem:                              # ← 真正能开始发请求的位置
        async with httpx.AsyncClient(...):
            async for attempt in AsyncRetrying(stop=stop_after_attempt(retry_max+1), ...):
                ...
```

`asyncio.wait_for` 起算时刻 = 协程被调度 ≈ 进入 batch 内 `asyncio.gather` 的瞬间，
但 Semaphore 容量只有 4，多余的协程会卡在 `async with sem:` 上不前进。
计时器照常推进，最终在排队过程中被 `wait_for` 直接 `cancel()`，
被 catch 成 `asyncio.TimeoutError` 后包装为 `upstream_timeout`。

**两个独立缺陷叠加**：
1. **计时维度错配**：业务上 timeout 应衡量「上游响应速度」，但代码上变成衡量「排队 + 上游」。
2. **错误码语义错误**：把客户端排队过载也归类为 `upstream_timeout`，并标记 `retryable=true`，
   会诱导上层调用方反复重试 → 进一步加剧排队，形成正反馈雪崩。

**修复思路（任选其一）**：

- **方案 A（推荐）**：把 `acquire` 移到 `wait_for` 外，对「真上游请求」单独计时。
  ```python
  sem = get_grok_semaphore()
  async with sem:                              # 排队不计时
      return await asyncio.wait_for(           # 只计 HTTP + 重试
          _do_request(...), timeout=timeout_seconds,
      )
  ```
- **方案 B**：保留现状但区分两类超时——`acquire` 超过预期返回
  `error.code = "queue_overflow"`、`retryable=true` 但建议降低并发；HTTP 超时才返回
  `upstream_timeout`。
- **方案 C（最小改动）**：在 `_safe_grok` 入口加 `await asyncio.wait_for(sem.acquire(), …)`
  分别量化排队与 HTTP，超时分类即可一并解决。

**验证用例**：把上述 6 个 task 重提，应该看到：
- 改前：第 3、4 批 16/16 报 `upstream_timeout`；
- 改后：要么全部 `ok`（仅延迟变长），要么前几批 `ok`、后几批报 `queue_overflow` 而非 `upstream_timeout`。

### 2.2 [P1 中] 对低质量短查询缺少早退路径，浪费 Semaphore 槽位

**现象**：batch `["", " ", "AAA", "BBB"]` 中 `AAA` / `BBB` 进入上游后，30s timeout 整齐失败。
说明上游 grok2api 在「无意义短串」上**会一直 streaming 直至 timeout**，期间占用一个 Semaphore 槽。
从客户端视角看，2 个无效 query 会在 30s 内瘫痪 50% 的 concurrency 容量。

**修复思路**：

- 客户端侧：在 `_perform_web_search` 入口对 `len(query.strip()) < 3` 或仅 ASCII 单词重复
  之类启发式预过滤，直接返回 `status=skipped`。
- 服务侧（grok2api）：streaming 在收到首个空 chunk 或 N 秒无新增 token 后主动断流。
- 至少把默认 timeout 在这种情况下显著缩短（比如统计 P95，并加 2× margin）。

### 2.3 [P2 低] batch 静默丢弃空 query，缺少回执提示

**现象**：传入 `queries=["", " ", "AAA", "BBB"]`，响应里 `batch_size=2`，没有任何
字段告知用户「2 个被丢弃」。返回数组长度从 4 变 2，调用方很难做下标对齐。

```json
{"batch_size": 2, "ok_count": 0, "error_count": 2, "results": [ ... 2 项 ... ]}
```

**修复思路**：在响应里增加：

```json
{
  "batch_size": 2,
  "input_size": 4,
  "dropped": [
    {"index": 0, "reason": "empty_query"},
    {"index": 1, "reason": "empty_query"}
  ],
  ...
}
```

或保持长度等于 input、把丢弃位写为 `{"status": "skipped", "reason": "empty_query"}`，
便于按下标对齐结果。

### 2.4 [P2 低] `web_fetch` 没有 timeout 参数，与 `web_search`/`batch` 行为不一致

`web_search` 和 `web_search_batch` 都接受 `timeout` 参数（Go-style duration），
但 `web_fetch` 没有，仅在 `_execute_stream_with_retry` 内部用 httpx `read=120s` 兜底
(`providers/grok.py:289`)。结合 P0 的 Semaphore 共享问题，意味着——

- 当 `batch` 把 4 个 Semaphore 槽全占满时，`web_fetch` 会无限期排队。
- 上层 AI 没有显式告知「fetch 超时」的方法，只能等 httpx read=120s 失败，体感非常差。

**修复思路**：给 `web_fetch` 增加同样的 `timeout` 参数 + 用相同的 `wait_for` 包装。
注意要先解决 P0，否则只是把同一个错误暴露到 fetch 路径。

### 2.5 [P3 低] `get_config_info` 的 connection_test 偶发误报「网络错误」

`get_config_info` 在压力测试开始时报告：
```json
"connection_test": {"status": "❌ 连接失败", "message": "网络错误: ", "response_time_ms": 0}
```

但同一秒内 `web_search` 实际成功。怀疑 connection_test 内部超时太短或与并发请求互相阻塞。
非阻塞但容易把使用者误导到「服务挂了」。建议：

- 给 connection_test 设置独立、更宽松的超时（≥10s）。
- 如果 client 池已饱和，至少把消息文案改为更精确的 `httpx.* (timeout/network)`，避免空 message 的尴尬输出。

## 3. 新版特性可用性确认（正面结论）

虽然有 P0 缺陷，新版整体设计是合理的，下述新特性在压力测试中得到正面验证：

- ✅ **异步任务流水线工作正常**：状态机 `queued → running → completed | failed | cancelled` 表现稳定，
  长轮询 (`wait="2m"`) 正确按 `_done_event` 唤醒，没有遗漏 done 信号。
- ✅ **空字符串被正确过滤**：batch 在调用 provider 前剔除空白 query，没有把 `""` 真的发出去。
- ✅ **短超时如实切断**：`timeout="3s"` 实测在 3.006s 后返回 `upstream_timeout`，误差 < 10ms。
- ✅ **`error.code` 模型实现到位**：`upstream_timeout` / `upstream_network_error` /
  `internal_error` / `config_error` / `invalid_model` 都已分类，便于上层调用方做差异化处理。
  **唯一的语义瑕疵就是 P0 中的「排队卡死被错分到 upstream_timeout」**。
- ✅ **进程级 Semaphore 共享**：`web_search` / `batch` / 异步 task 共用同一信号量，
  对 grok2api 后端的保护是到位的——这反而是 P0 缺陷的另一面。

## 4. 建议优先级

1. **本次必修（P0）**：方案 A 或 C，把 acquire 移出 `wait_for`，停止把排队识别为上游超时。
2. **下次迭代（P1）**：低质量 query 早退或缩短默认 timeout，避免后端被 AAA/BBB 类查询绑死 30s+ 槽位。
3. **可选（P2）**：batch 增加 `dropped` 元数据；`web_fetch` 暴露 timeout 参数；
   responses provider 的相同代码路径如果有同样问题需一并审查。
4. **观察（P3）**：connection_test 的偶发误报。

## 5. 数据采集元信息（便于复现）

- 提交时序与失败率随提交顺序单调递增，是 Semaphore 排队失效的强证据，可重现度 100%。
- 第 3、第 4 批 16 个 `error.timeout_seconds=60.0` 且 `finished_at - submitted_at ≈ 60.04s`，
  误差 < 50ms，说明是 `wait_for` 整齐切断，而不是上游异常分布。
- 不需要外部抓包；下次复现可在 `_perform_web_search` 入口和 `_execute_stream_with_retry`
  acquire 前后各加一行 `log_info(..., f"phase=acquire_wait/sec={t}")`，
  几行打点就能在生产日志里区分排队和上游响应。
