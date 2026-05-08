# GrokSearch MCP Deployment Trap 诊断报告

- 时间：2026-05-08 15:12:23 UTC+08:00
- 触发任务：commit 1-7 完成后用户要求"用最新版本进行 debug 测试找问题"
- 工作产物：`tools/validation/debug_latest_stdio.py`、`tools/validation/inspect_tool_schema.py`

## 结论

`commit 1-7` 的产品行为在 **真实上游 grok2api + grok-4.20-fast** 模型下全部通过验证；
但本次 debug 找到了**一个真正的 P0 部署陷阱**，导致用户 Windsurf 实例在
此前完全没有跑到任何修复代码——所有 commit 的实际产品收益为零。

## 🚨 P0: `uvx --refresh --from <local path>` 不重新构建本地源码改动

### 现场

`@C:/Users/GRM/.codeium/windsurf/mcp_config.json` 原 `grok-search` 配置：

```json
"grok-search": {
  "args": ["--refresh", "--from", "C:/projects/grok/GrokSearch", "grok-search"],
  "command": "uvx",
  ...
}
```

### 客观证据

启动子进程后注入诊断脚本，检查实际加载的 `web_search_batch` 函数签名：

```text
SIG: ['queries', 'platform', 'model', 'extra_sources', 'timeout']
SRC: C:\Users\GRM\AppData\Local\uv\cache\archive-v0\KAUWyqKEf3vkiY1lTShG6
     \Lib\site-packages\grok_search\server.py
```

- 缺 `auto_async_threshold` 参数（commit `1947994` 加入）。
- 加载位置在 `%LOCALAPPDATA%\uv\cache\archive-v0\<hash>` 而不是
  `C:/projects/grok/GrokSearch/src/grok_search/`。
- 缓存 `server.py` mtime = `2026-05-08 10:31:39`，**早于 commit 7 (1947994)**。

`fastmcp` 在子进程启动时 stderr 抛：

```text
[05/08/26 14:56:58] Error validating tool 'web_search_batch'
ValidationError: 1 validation error for call[web_search_batch]
auto_async_threshold
  Unexpected keyword argument [type=unexpected_keyword_argument, ...]
```

### 已尝试且无效的修法

| 尝试 | 结果 |
|------|------|
| `uvx --reinstall` | uvx 不支持此 flag |
| 第二次启动观察 | 仍命中同一 archive cache |
| 手动 `uv build` 出 wheel 到 dist/ | uvx 用自己 archive，不读 dist/ |

### 已验证可工作的修法

```json
"grok-search": {
  "args": [],
  "command": "C:/projects/grok/GrokSearch/.venv/Scripts/grok-search.exe",
  ...
}
```

前置：在仓库根执行过一次 `uv pip install -e .`（editable 模式）。
之后每次源码改动 `.venv` 入口立即生效，无需重启 Windsurf 之外的任何步骤。

验证 `list_tools` 返回的 `web_search_batch` schema：

```text
web_search_batch params: ['auto_async_threshold', 'extra_sources', 'model',
                          'platform', 'queries', 'timeout']
auto_async_threshold present: True
schema.default = -1, type = integer
```

### 影响面

- 用户 Windsurf 中**所有过去对 GrokSearch 源码的改动**只有在手动执行
  `uv cache prune --force` 并重启 Windsurf 后才生效；这从未在工作区文档里
  说明过。
- `commit 1-7` 在真实生产实例（即用户的 Windsurf）上**未生效**——本次 debug
  是第一次让真实版本跑起来。
- 单元测试（`pytest`）跑的是 editable 的 src 路径，所以 100/100 全过；这
  解释了"测试全过 + 用户感觉没改"的认知差。

### 修复动作

- ✅ 已修改 `c:\Users\GRM\.codeium\windsurf\mcp_config.json`：command 改为
  `.venv/Scripts/grok-search.exe`，args 清空，env 保留。
- ⏳ **用户必须在 Windsurf 里 reload MCP servers 或重启 Windsurf**，才能让
  新 mcp_config 生效。这步无法自动完成。

## 真实场景验收（命令：`uv run python tools/validation/debug_latest_stdio.py`）

下列 8 个场景在 `.venv/Scripts/grok-search.exe` + 真实 grok2api 上游下全部通过：

| # | 场景 | 状态 | 关键观测 |
|---|------|------|----------|
| 01 | `get_config_info` | ✅ 0.52s | 配置完整，API Key 正确脱敏成 `sk-a**anoh` |
| 02 | `web_search` 单 query | ✅ 12.6s | 返回 Claude Opus 4.7 真实答案，6 个引用源 |
| 03 | `web_search` `extra_sources=3`（tavily 未配） | ✅ 12.1s | 不抛异常，warning 字段缺失因为 tavily/firecrawl 都没配 key（这个 commit 1 的 surface 路径只在配了 key 时触发，本次未覆盖到） |
| 04 | `web_search_batch` 4q 同步 | ✅ 14.7s | `cancelled_count=0` 字段存在（commit 3） |
| 05 | `web_search_batch auto_async_threshold=3` 6q | ✅ 提交 0.03s | `status="submitted"`、`task_id`、`hint` 字段完整（commit 7） |
| 05.b | `get_search_task_result wait="120s"` | ✅ 20.5s 完成 | `state="completed"`，`ok_count=6`（commit 6 持久化 + commit 7 异步路由打通） |
| 06 | `submit_search_task` + `cancel_search_task` | ✅ 0.33s 完成取消 | `cancel_hint="debug-cancel"` 正确写入 |
| 07 | 4 个输入校验 case | ✅ | `invalid_kind` / `invalid_params` / `queries_empty` / `task_not_found` 全部结构化错误，无 MCP 异常逃逸 |
| 08 | `list_search_tasks` | ✅ | 看到 completed + cancelled 两条，含完整 result |

## 留下的次要观察

| 严重度 | 项 | 说明 |
|--------|-----|------|
| P2 | `_mask_api_key` 把 `sk-aomanoh` 显示为 `sk-a**anoh` | 12 字符以下 key 脱敏可读性差，但不是安全问题 |
| P2 | `extra_sources>0` 但所有 provider 都未配置 key 时 | 调用静默 ok，未给 warning 提示"你设了 extra_sources 但没启 provider"。可在 commit 1 的 surface 路径前加一条预检 warning |
| P3 | Windows GBK stdout 默认会让含 ✅ 的 `get_config_info` 在某些客户端打印崩溃 | 已在 debug 脚本里 `reconfigure(encoding="utf-8")`，但 server 端用 ASCII 标记可能更鲁棒 |

## Action Items

- [x] mcp_config.json 修复
- [ ] 用户 reload Windsurf MCP（手动）
- [ ] 后续 PR：在 README / handoff 文档加一段 "本地开发部署"，明确告知
      `--refresh` 不刷本地包，必须用 editable + venv 入口或显式 `uv cache prune`
