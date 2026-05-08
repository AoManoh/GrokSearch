"""用最新本地代码启动 grok-search stdio MCP server，跑真实 debug 场景找问题。

不写入 docs/，输出全部打到 stdout 与 stderr。退出码：
- 0：所有场景符合预期
- 1：发现至少一个值得追查的异常或行为偏差

运行：
    uv run python tools/validation/debug_latest_stdio.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# Windows GBK stdout 会在打印 ✅ 等 unicode 时炸，强制 utf-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_for_subprocess() -> dict[str, str]:
    """复刻 Windsurf mcp_config.json 里的 grok-search 环境，只覆盖必要项。

    GROK_API_URL / GROK_API_KEY / GROK_MODEL 从当前进程继承（用户已 export）。
    若未设置则从 mcp_config.json 读默认值（保持与生产一致）。
    """
    env = os.environ.copy()
    # 与 Windsurf 现网配置保持一致
    env.setdefault("GROK_API_URL", "https://grok.aomanoh.tech/v1")
    env.setdefault("GROK_API_KEY", "sk-aomanoh")
    env.setdefault("GROK_MODEL", "grok-4.20-fast")
    env.setdefault("GROK_SEARCH_PROVIDER", "chat")
    env.setdefault("GROK_CONCURRENCY", "4")
    # 持久化路径用本次专用临时目录，便于 replay 测试
    env["GROK_TASK_STORE_PATH"] = str(PROJECT_ROOT / "tools" / "validation" / ".debug-tasks.jsonl")
    # 让 stderr 日志可见
    env["GROK_LOG_LEVEL"] = "INFO"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _make_transport() -> StdioTransport:
    """直接通过 venv 的 grok-search 入口启动 stdio server，绕过 uvx cache 陷阱。

    P0 实测发现 ``uvx --refresh --from <local path>`` 不会重新构建本地源码改动，
    会持续命中 ``%LOCALAPPDATA%/uv/cache/archive-v0/<hash>`` 里的旧 wheel；用
    venv 入口或 ``uv run`` 才能保证拿到 ``pip install -e .`` 的实时源码。
    """
    venv_exe = PROJECT_ROOT / ".venv" / "Scripts" / "grok-search.exe"
    if venv_exe.exists():
        return StdioTransport(
            command=str(venv_exe),
            args=[],
            env=_env_for_subprocess(),
            keep_alive=False,
        )
    return StdioTransport(
        command="uv",
        args=["run", "--directory", str(PROJECT_ROOT), "grok-search"],
        env=_env_for_subprocess(),
        keep_alive=False,
    )


def _extract_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}
    return {"_empty": True}


async def _call(client: Client, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    try:
        result = await client.call_tool(name, args)
        return _extract_payload(result), time.perf_counter() - started
    except Exception as exc:
        return {
            "_exception": True,
            "type": type(exc).__name__,
            "message": str(exc) or repr(exc),
            "traceback": traceback.format_exc(),
        }, time.perf_counter() - started


def _print_block(title: str, payload: Any, elapsed: float | None = None) -> None:
    sys.stdout.write("\n" + "=" * 78 + "\n")
    if elapsed is not None:
        sys.stdout.write(f"[{title}] elapsed={elapsed:.2f}s\n")
    else:
        sys.stdout.write(f"[{title}]\n")
    sys.stdout.write("-" * 78 + "\n")
    if isinstance(payload, (dict, list)):
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2)[:4000] + "\n")
    else:
        sys.stdout.write(str(payload)[:4000] + "\n")
    sys.stdout.flush()


# ====================== 诊断场景 ======================


async def scenario_health(client: Client, findings: list[dict]) -> None:
    payload, elapsed = await _call(client, "get_config_info", {})
    _print_block("01 health get_config_info", payload, elapsed)
    if "_exception" in payload:
        findings.append({
            "id": "health_exception",
            "severity": "P0",
            "msg": "get_config_info 抛 MCP 异常",
            "detail": payload,
        })
        return
    test = payload.get("connection_test") or {}
    if test.get("success") is False:
        findings.append({
            "id": "health_connection_failed",
            "severity": "P1",
            "msg": "/models 探活失败",
            "detail": test,
        })


async def scenario_single_query(client: Client, findings: list[dict]) -> None:
    payload, elapsed = await _call(client, "web_search", {
        "query": "Anthropic Claude latest release notes",
        "timeout": "60s",
    })
    _print_block("02 single web_search", payload, elapsed)
    if "_exception" in payload:
        findings.append({
            "id": "single_query_exception",
            "severity": "P0",
            "msg": "web_search 抛 MCP 异常",
            "detail": payload,
        })
        return
    status = payload.get("status")
    if status not in {"ok", "partial", "empty", "skipped"}:
        findings.append({
            "id": "single_query_unexpected_status",
            "severity": "P1",
            "msg": f"非预期 status={status}",
            "detail": payload,
        })


async def scenario_extra_failures(client: Client, findings: list[dict]) -> None:
    """配置 extra_sources>0 但 tavily/firecrawl 没启用，看是否触发 commit 1 的诊断字段。"""
    payload, elapsed = await _call(client, "web_search", {
        "query": "FastAPI vs Flask 2026 performance",
        "extra_sources": 3,
        "timeout": "60s",
    })
    _print_block("03 web_search extra_sources=3", payload, elapsed)
    if "_exception" in payload:
        findings.append({
            "id": "extra_failures_exception",
            "severity": "P1",
            "msg": "extra_sources 调用抛异常",
            "detail": payload,
        })


async def scenario_batch_sync(client: Client, findings: list[dict]) -> None:
    """4 query batch 同步执行，验证 P0 fix 后排队等待不算 timeout。"""
    queries = [
        "latest news on autonomous driving regulation",
        "Apple Vision Pro adoption rate 2026",
        "OpenAI Sora video generation latest demos",
        "Google Gemini 2 enterprise rollout status",
    ]
    payload, elapsed = await _call(client, "web_search_batch", {
        "queries": queries,
        "timeout": "60s",
        "auto_async_threshold": 0,  # 强制同步
    })
    _print_block("04 web_search_batch 4q sync", payload, elapsed)
    if "_exception" in payload:
        findings.append({
            "id": "batch_sync_exception",
            "severity": "P0",
            "msg": "batch 同步路径抛 MCP 异常",
            "detail": payload,
        })
        return
    if "cancelled_count" not in payload:
        findings.append({
            "id": "batch_missing_cancelled_count",
            "severity": "P2",
            "msg": "commit 3 应保证 batch 响应里有 cancelled_count 字段",
            "detail": {k: payload.get(k) for k in ("ok_count", "error_count", "cancelled_count")},
        })


async def scenario_batch_auto_async(client: Client, findings: list[dict]) -> None:
    """6 query + auto_async_threshold=3 应立即返回 task_id（commit 7）。"""
    queries = [f"latest stable diffusion variant {i}" for i in range(6)]
    payload, elapsed = await _call(client, "web_search_batch", {
        "queries": queries,
        "timeout": "60s",
        "auto_async_threshold": 3,
    })
    _print_block("05 web_search_batch auto_async", payload, elapsed)
    if "_exception" in payload:
        findings.append({
            "id": "auto_async_exception",
            "severity": "P0",
            "msg": "auto_async 路径抛 MCP 异常",
            "detail": payload,
        })
        return
    if payload.get("status") != "submitted":
        findings.append({
            "id": "auto_async_did_not_route",
            "severity": "P1",
            "msg": "应触发异步路由但 status != 'submitted'",
            "detail": payload,
        })
        return
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("task-"):
        findings.append({
            "id": "auto_async_missing_task_id",
            "severity": "P0",
            "msg": "异步路由没返回 task_id",
            "detail": payload,
        })
        return
    if elapsed > 1.5:
        findings.append({
            "id": "auto_async_submit_slow",
            "severity": "P2",
            "msg": f"异步 submit 耗时 {elapsed:.2f}s 超过 1.5s 预期",
        })
    # 长轮询拿最终结果
    final, final_elapsed = await _call(client, "get_search_task_result", {
        "task_id": task_id,
        "wait": "120s",
    })
    _print_block(f"05.b get_search_task_result {task_id}", final, final_elapsed)
    if final.get("state") not in {"completed", "failed", "cancelled"}:
        findings.append({
            "id": "auto_async_did_not_finish",
            "severity": "P1",
            "msg": f"task 未在 120s 内到达 terminal: state={final.get('state')}",
            "detail": final,
        })


async def scenario_submit_then_cancel(client: Client, findings: list[dict]) -> None:
    """submit 一个 batch，立即 cancel，验证 commit 3/6：cancel 状态正确 + JSONL 写入。"""
    submit_payload, _ = await _call(client, "submit_search_task", {
        "kind": "web_search_batch",
        "params": {
            "queries": [
                "long-running query for cancellation test 1",
                "long-running query for cancellation test 2",
                "long-running query for cancellation test 3",
            ],
            "timeout": "120s",
        },
    })
    _print_block("06 submit before cancel", submit_payload)
    task_id = submit_payload.get("task_id")
    if not isinstance(task_id, str):
        findings.append({
            "id": "submit_missing_task_id",
            "severity": "P0",
            "msg": "submit_search_task 没返回 task_id",
            "detail": submit_payload,
        })
        return
    # 给 server 一点时间让任务进入 running
    await asyncio.sleep(0.3)
    cancel_payload, _ = await _call(client, "cancel_search_task", {
        "task_id": task_id,
        "hint": "debug-cancel",
    })
    _print_block("06.b cancel_search_task", cancel_payload)
    if cancel_payload.get("state") != "cancelled":
        findings.append({
            "id": "cancel_did_not_finalize",
            "severity": "P1",
            "msg": f"cancel 后 state={cancel_payload.get('state')} 不是 cancelled",
            "detail": cancel_payload,
        })


async def scenario_invalid_inputs(client: Client, findings: list[dict]) -> None:
    """触发已知的输入校验路径，看错误码是否结构化（commit 1 / 7 后的 invariant）。"""
    cases = [
        ("invalid_kind", "submit_search_task", {"kind": "garbage", "params": {}}),
        ("missing_query", "submit_search_task", {"kind": "web_search", "params": {}}),
        ("empty_queries_list", "web_search_batch", {"queries": []}),
        ("unknown_task", "get_search_task_result", {"task_id": "task-doesnotexist", "wait": "0s"}),
    ]
    for case_id, tool, args in cases:
        payload, elapsed = await _call(client, tool, args)
        _print_block(f"07 {case_id} {tool}", payload, elapsed)
        if "_exception" in payload:
            findings.append({
                "id": f"validation_exception_{case_id}",
                "severity": "P1",
                "msg": f"输入校验 case={case_id} 让 MCP 抛异常而非返回结构化错误",
                "detail": payload,
            })


async def scenario_tasks_listing(client: Client, findings: list[dict]) -> None:
    payload, elapsed = await _call(client, "list_search_tasks", {})
    _print_block("08 list_search_tasks", payload, elapsed)
    if "_exception" in payload:
        findings.append({
            "id": "list_tasks_exception",
            "severity": "P1",
            "msg": "list_search_tasks 抛异常",
            "detail": payload,
        })


# ====================== runner ======================


async def main() -> int:
    findings: list[dict] = []
    transport = _make_transport()
    sys.stdout.write(f"[bootstrap] starting stdio child via uvx, project={PROJECT_ROOT}\n")
    sys.stdout.flush()
    try:
        async with Client(transport) as client:
            tools = await client.list_tools()
            tool_names = sorted(t.name for t in tools)
            _print_block("00 available tools", tool_names)
            scenarios = [
                scenario_health,
                scenario_single_query,
                scenario_extra_failures,
                scenario_batch_sync,
                scenario_batch_auto_async,
                scenario_submit_then_cancel,
                scenario_invalid_inputs,
                scenario_tasks_listing,
            ]
            for scenario in scenarios:
                try:
                    await scenario(client, findings)
                except Exception as exc:
                    findings.append({
                        "id": f"scenario_crash_{scenario.__name__}",
                        "severity": "P0",
                        "msg": f"{scenario.__name__} 抛未捕获异常",
                        "detail": {"type": type(exc).__name__, "message": str(exc)},
                    })
                    sys.stdout.write(f"[crash] {scenario.__name__}: {exc}\n")
                    traceback.print_exc()
    except Exception as exc:
        findings.append({
            "id": "client_bootstrap_fail",
            "severity": "P0",
            "msg": f"无法连接子进程 MCP: {type(exc).__name__}: {exc}",
            "detail": traceback.format_exc(),
        })

    sys.stdout.write("\n" + "#" * 78 + "\n# Findings\n" + "#" * 78 + "\n")
    if not findings:
        sys.stdout.write("(无)\n")
        return 0
    for f in findings:
        sys.stdout.write(f"\n[{f['severity']}] {f['id']}: {f['msg']}\n")
        if "detail" in f:
            sys.stdout.write(json.dumps(f["detail"], ensure_ascii=False, indent=2)[:1500] + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
