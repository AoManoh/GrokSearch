![这是图片](./images/title.png)
<div align="center">

<!-- # Grok Search MCP -->

[English](./docs/README_EN.md) | 简体中文

**Grok-with-Tavily MCP，为 Claude Code 提供更完善的网络访问能力**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.0.0+-green.svg)](https://github.com/jlowin/fastmcp)

</div>

---

## 一、概述

Grok Search MCP 是一个基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建的 MCP 服务器，采用**双引擎架构**：**Grok** 负责 AI 驱动的智能搜索，**Tavily** 负责高保真网页抓取与站点映射，各取所长为 Claude Code / Cherry Studio 等LLM Client提供完整的实时网络访问能力。

```
Claude ──MCP──► Grok Search Server
                  ├─ web_search  ───► Grok API（AI 搜索）
                  ├─ web_fetch   ───► Tavily Extract → Firecrawl Scrape（内容抓取，自动降级）
                  └─ web_map     ───► Tavily Map（站点映射）
```

### 当前 Fork 维护边界

- `AoManoh/GrokSearch` 由当前维护团队独立维护，是当前工作区内 `GrokSearch` 能力、文档、验收结论和后续优化计划的事实源。
- 本仓库默认不会向原作者仓库发起 PR；后续功能演进、稳定性修复和发布节奏以当前 fork 自身的需求和验证结果为准。
- 如需参考原作者仓库或其他上游实现，应作为差异对照和思路来源，而不是当前开发任务的默认提交目标。

### 功能特性

- **双引擎**：Grok 搜索 + Tavily 抓取/映射，互补协作
- **Firecrawl 托底**：Tavily 提取失败时自动降级到 Firecrawl Scrape，支持空内容自动重试
- **OpenAI 兼容接口**，支持任意 Grok 镜像站
- **自动时间注入**（检测时间相关查询，注入本地时间上下文）
- 一键禁用 Claude Code 官方 WebSearch/WebFetch，强制路由到本工具
- 智能重试（支持 Retry-After 头解析 + 指数退避）
- 父进程监控（Windows 下自动检测父进程退出，防止僵尸进程）

### 效果展示
我们以在`cherry studio`中配置本MCP为例，展示了`claude-opus-4.6`模型如何通过本项目实现外部知识搜集，降低幻觉率。
![](./images/wogrok.png)
如上图，**为公平实验，我们打开了claude模型内置的搜索工具**，然而opus 4.6仍然相信自己的内部常识，不查询FastAPI的官方文档，以获取最新示例。
![](./images/wgrok.png)
如上图，当打开`grok-search MCP`时，在相同的实验条件下，opus 4.6主动调用多次搜索，以**获取官方文档，回答更可靠。** 


## 二、安装

### 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（推荐的 Python 包管理器）
- Claude Code

<details>
<summary><b>安装 uv</b></summary>

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> Windows 用户**强烈推荐**在 WSL 中运行本项目。

</details>

### 一键安装
若之前安装过本项目，使用以下命令卸载旧版MCP。
```
claude mcp remove grok-search
```


将以下命令中的环境变量替换为你自己的值后执行。Grok 接口需为 OpenAI 兼容格式；Tavily 为可选配置，未配置时工具 `web_fetch` 和 `web_map` 不可用。

如果上游使用 `grok2api`，则：

- `GROK_API_URL=http(s)://<grok2api-host>/v1`
- `GROK_API_KEY=<grok2api api key>`

若要从当前工作区导出一份跨机器、跨 OS 可复用的 MCP JSON，可执行：

```bash
uv run --directory /path/to/grok2api python scripts/export_groksearch_mcp_json.py \
  --client-transport stdio \
  --stdio-launcher portable \
  --base-url https://your-grok2api-host \
  --api-key your-grok2api-api-key
```

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/AoManoh/GrokSearch@main",
    "grok-search"
  ],
  "env": {
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com"
  }
}'
```

<details> <summary>如果遇到 SSL / 证书验证错误</summary>

在部分企业网络或代理环境中，可能会出现类似错误：

certificate verify failed
self signed certificate in certificate chain

可以在 uvx 参数中添加 --native-tls，使其使用系统证书库：

claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--native-tls",
    "--from",
    "git+https://github.com/AoManoh/GrokSearch@main",
    "grok-search"
  ],
  "env": {
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com"
  }
}'
</details> 

<details>
<summary><b>完整推荐配置参考（含 Tavily / Firecrawl 与代理隔离）</b></summary>

适用于同时启用 Grok 搜索 + Tavily 抓取/映射 + Firecrawl 托底，并希望**不受用户操作系统代理影响**的场景。可直接复制到 Windsurf / Claude Code / Cherry Studio 等 MCP Host 的配置文件中：

```json
{
  "grok-search": {
    "command": "uvx",
    "args": [
      "--from",
      "git+https://github.com/AoManoh/GrokSearch@main",
      "grok-search"
    ],
    "disabled": false,
    "env": {
      "GROK_API_URL": "https://your-grok2api-host/v1",
      "GROK_API_KEY": "your-key",
      "GROK_MODEL": "grok-4.20-fast",
      "GROK_SEARCH_PROVIDER": "chat",
      "TAVILY_API_KEY": "tvly-your-tavily-key",
      "TAVILY_ENABLED": "true",
      "FIRECRAWL_API_KEY": "fc-your-firecrawl-key",
      "HTTP_PROXY": "",
      "HTTPS_PROXY": "",
      "ALL_PROXY": "",
      "NO_PROXY": "*"
    }
  }
}
```

要点：

- `GROK_SEARCH_PROVIDER=chat` 强制走 OpenAI 兼容 `/chat/completions`，避免 `auto` 在 hostname 命中 `api.x.ai` 时切到 `/responses`
- `TAVILY_API_KEY` / `FIRECRAWL_API_KEY` 不要留空字符串，留空等同未设
- `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 用于把子进程的出站代理与用户系统代理解耦，详见下方常见问题

</details>

除此之外，你还可以在`env`字段中配置更多环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `GROK_API_URL` | ✅ | - | Grok API 地址（OpenAI 兼容格式） |
| `GROK_API_KEY` | ✅ | - | Grok API 密钥 |
| `GROK_MODEL` | ❌ | `grok-4.1-fast` | 默认模型（设置后优先于 `~/.config/grok-search/config.json`） |
| `TAVILY_API_KEY` | ❌ | - | Tavily API 密钥（用于 web_fetch / web_map） |
| `TAVILY_API_URL` | ❌ | `https://api.tavily.com` | Tavily API 地址 |
| `TAVILY_ENABLED` | ❌ | `true` | 是否启用 Tavily；设为 `false` 后运行时将完全跳过 Tavily |
| `FIRECRAWL_API_KEY` | ❌ | - | Firecrawl API 密钥（Tavily 失败时托底） |
| `FIRECRAWL_API_URL` | ❌ | `https://api.firecrawl.dev/v2` | Firecrawl API 地址 |
| `GROK_DEBUG` | ❌ | `false` | 调试模式 |
| `GROK_LOG_LEVEL` | ❌ | `INFO` | 日志级别 |
| `GROK_LOG_DIR` | ❌ | `logs` | 日志目录 |
| `GROK_RETRY_MAX_ATTEMPTS` | ❌ | `3` | 最大重试次数 |
| `GROK_RETRY_MULTIPLIER` | ❌ | `1` | 重试退避乘数 |
| `GROK_RETRY_MAX_WAIT` | ❌ | `10` | 重试最大等待秒数 |
| `HTTP_PROXY` | ❌ | - | HTTP 出站代理（例如 `http://127.0.0.1:7890`） |
| `HTTPS_PROXY` | ❌ | - | HTTPS 出站代理（例如 `http://127.0.0.1:7890`） |
| `ALL_PROXY` | ❌ | - | 全协议出站代理 |
| `NO_PROXY` | ❌ | - | 跳过代理的主机列表（逗号分隔，`*` 表示全部绕过） |

> 提示：本服务所有外发请求均使用 `httpx`（默认 `trust_env=True`），会自动读取以上代理变量。MCP 子进程会继承启动方（如 Windsurf / Claude Code）的系统代理；如需隔离，请在 MCP 配置 `env` 中显式声明，详见下方"常见问题"。

当前仓库也提供了可直接复制的环境样例：[`.env.example`](./.env.example)。

### 远程 HTTP MCP 服务

若你希望把 `GrokSearch` 部署成共享远程 MCP，而不是本地 `stdio` 进程，请参考 [docs/REMOTE_MCP_SERVER.md](./docs/REMOTE_MCP_SERVER.md)。

当上游使用 `grok2api` 时，默认 `GET /ready` 只验证 `/v1/models`。如果你要把 readiness 绑定到真实 chat/search 可用性，请额外设置：

```bash
export MCP_READY_CHECKS="models,chat"
```

这样 `/ready` 会额外发起一次最小流式聊天探针，并在 `403`、`502` 这类上游失败时返回结构化 `upstream_status`。


### 验证安装

```bash
claude mcp list
```

🍟 显示连接成功后，我们**十分推荐**在 Claude 对话中输入 
```
调用 grok-search toggle_builtin_tools，关闭Claude Code's built-in WebSearch and WebFetch tools
```
工具将自动修改**项目级** `.claude/settings.json` 的 `permissions.deny`，一键禁用 Claude Code 官方的 WebSearch 和 WebFetch，从而迫使claude code调用本项目实现搜索！



## 三、MCP 工具介绍

<details>
<summary>本项目提供八个 MCP 工具（展开查看）</summary>

### `web_search` — AI 网络搜索

通过 Grok API 执行 AI 驱动的网络搜索，默认仅返回 Grok 的回答正文，并返回 `session_id` 以便后续获取信源。

`web_search` 输出不展开信源，仅返回 `sources_count`；信源会按 `session_id` 缓存在服务端，可用 `get_sources` 拉取。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | - | 搜索查询语句 |
| `platform` | string | ❌ | `""` | 聚焦平台（如 `"Twitter"`, `"GitHub, Reddit"`） |
| `model` | string | ❌ | `null` | 按次指定 Grok 模型 ID |
| `extra_sources` | int | ❌ | `0` | 额外补充信源数量（Tavily/Firecrawl，可为 0 关闭） |

自动检测查询中的时间相关关键词（如"最新""今天""recent"等），注入本地时间上下文以提升时效性搜索的准确度。

返回值（结构化字典）：
- `session_id`: 本次查询的会话 ID
- `status`: `ok` 或 `error`
- `content`: Grok 回答正文（已自动剥离信源）
- `sources_count`: 已缓存的信源数量
- `error`: 仅失败时返回，包含 `code` / `message` / `provider` / `upstream_status`

### `get_sources` — 获取信源

通过 `session_id` 获取对应 `web_search` 的全部信源。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | ✅ | `web_search` 返回的 `session_id` |

返回值（结构化字典）：
- `session_id`
- `sources_count`
- `sources`: 信源列表（每项包含 `url`，可能包含 `title`/`description`/`provider`）

### `web_fetch` — 网页内容抓取

通过 Tavily Extract API 获取完整网页内容，返回 Markdown 格式。Tavily 失败时自动降级到 Firecrawl Scrape 进行托底抓取。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | ✅ | 目标网页 URL |

### `web_map` — 站点结构映射

通过 Tavily Map API 遍历网站结构，发现 URL 并生成站点地图。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | string | ✅ | - | 起始 URL |
| `instructions` | string | ❌ | `""` | 自然语言过滤指令 |
| `max_depth` | int | ❌ | `1` | 最大遍历深度（1-5） |
| `max_breadth` | int | ❌ | `20` | 每页最大跟踪链接数（1-500） |
| `limit` | int | ❌ | `50` | 总链接处理数上限（1-500） |
| `timeout` | int | ❌ | `150` | 超时秒数（10-150） |

### `get_config_info` — 配置诊断

无需参数。显示所有配置状态、测试 Grok API 连接、返回响应时间和可用模型列表（API Key 自动脱敏）。

### `switch_model` — 模型切换

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | ✅ | 模型 ID（如 `"grok-4.1-fast"`, `"grok-4"`） |

切换后配置持久化到 `~/.config/grok-search/config.json`，跨会话保持。

### `toggle_builtin_tools` — 工具路由控制

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `action` | string | ❌ | `"status"` | `"on"` 禁用官方工具 / `"off"` 启用官方工具 / `"status"` 查看状态 |

修改项目级 `.claude/settings.json` 的 `permissions.deny`，一键禁用 Claude Code 官方的 WebSearch 和 WebFetch。

### `search_planning` — 搜索规划

结构化搜索规划脚手架（分阶段、多轮），用于在执行复杂搜索前先生成可执行的搜索计划。
</details>

## 四、常见问题

<details>
<summary>
Q: 必须同时配置 Grok 和 Tavily 吗？
</summary>
A: Grok（`GROK_API_URL` + `GROK_API_KEY`）为必填，提供核心搜索能力。Tavily 和 Firecrawl 均为可选：配置 Tavily 后 `web_fetch` 优先使用 Tavily Extract，失败时降级到 Firecrawl Scrape；两者均未配置时 `web_fetch` 将返回配置错误提示。`web_map` 依赖 Tavily。
</details>

<details>
<summary>
Q: Grok API 地址需要什么格式？
</summary>
A: 需要 OpenAI 兼容格式的 API 地址（支持 `/chat/completions` 和 `/models` 端点）。如使用官方 Grok，需通过兼容 OpenAI 格式的镜像站访问。
</details>

<details>
<summary>
Q: 如何验证配置？
</summary>
A: 在 Claude 对话中说"显示 grok-search 配置信息"，将自动测试 API 连接并显示结果。
</details>

<details>
<summary>
Q: 偶发 ConnectError / 调用结果被系统代理影响怎么办？
</summary>

本服务内部使用 `httpx` 且未关闭 `trust_env`，因此会自动读取启动进程的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`。当 Claude Code、Windsurf、Cherry Studio 等 MCP Host 启动 `uvx grok-search` 子进程时，会**继承操作系统层面的代理变量**，可能与目标上游域名（例如国内自建 `grok2api`）冲突，表现为偶发 `ConnectError` 或上游不可达。

在 MCP 配置 `env` 中显式覆盖即可隔离：

**场景 1：上游在国内或局域网，需要绕过用户全局代理**

```json
"env": {
  "GROK_API_URL": "https://your-grok2api-host/v1",
  "GROK_API_KEY": "your-key",
  "HTTP_PROXY": "",
  "HTTPS_PROXY": "",
  "ALL_PROXY": "",
  "NO_PROXY": "*"
}
```

也可以精细放行，仅对相关域名绕过：

```json
"NO_PROXY": "your-grok2api-host,api.tavily.com,api.firecrawl.dev"
```

**场景 2：MCP 子进程需要走专用代理**

```json
"env": {
  "HTTPS_PROXY": "http://127.0.0.1:7890",
  "HTTP_PROXY": "http://127.0.0.1:7890",
  "NO_PROXY": "127.0.0.1,localhost,your-grok2api-host"
}
```

**场景 3：完全沿用系统代理**

不在 `env` 中声明任何代理变量即可，`httpx` 会自动从父进程环境读取。

> 在某些操作系统下，向子进程传入空字符串可能等同于未设置；建议同时显式声明 `NO_PROXY=*` 以确保绕过。

</details>

## 许可证

[MIT License](LICENSE)

---

<div align="center">

**如果这个项目对您有帮助，请给个 Star！**

[![Star History Chart](https://api.star-history.com/svg?repos=AoManoh/GrokSearch&type=date&legend=top-left)](https://www.star-history.com/#AoManoh/GrokSearch&type=date&legend=top-left)
</div>
