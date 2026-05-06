from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport


QUERY_BANK: list[str] = [
    "AIGC 最新行业应用和监管趋势",
    "论文降重 合规方法和学术诚信要求",
    "今天人工智能芯片市场最新动态",
    "最近大模型开源项目有哪些重要更新",
    "检索扩展生成 RAG 最新综述论文",
    "多模态大模型评测基准 最新研究",
    "FastAPI 生产环境部署最佳实践 2026",
    "PostgreSQL 向量检索 pgvector 性能优化",
    "中国新能源汽车出口 近期数据",
    "跨境电商独立站 最新增长策略",
    "欧盟人工智能法案 最新实施进展",
    "中国生成式人工智能服务管理 暂行办法 解读",
    "latest updates from xAI this week",
    "current state of AI agent benchmarks",
    "recent papers on retrieval augmented generation evaluation",
    "Kubernetes Gateway API production best practices",
    "NVIDIA latest earnings AI datacenter revenue",
    "global EV battery market trends latest",
    "EU AI Act implementation timeline latest",
    "US AI safety regulation latest executive actions",
    "WebGPU support status in major browsers",
    "SQLite vector search extensions comparison",
    "AI 搜索 search grounding citations 最新实践",
    "Claude Code Windsurf Cascade MCP server best practices",
    "GitHub trending agentic coding tools",
    "Reddit discussions about Grok API reliability",
    "Twitter X latest xAI Grok release notes",
    "Bitcoin ETF inflows latest weekly data",
    "Federal Reserve latest interest rate decision",
    "latest James Webb Space Telescope discoveries",
    "latest critical CVEs in Linux kernel",
    "Next.js latest release breaking changes",
]


TERMINAL_STATES = {"completed", "failed", "cancelled"}
UPSTREAM_CODES = {
    "upstream_timeout",
    "upstream_network_error",
    "upstream_http_error",
    "upstream_error",
}
SERVICE_CODES = {"internal_error", "config_error", "invalid_model"}


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
            return {"status": "unknown", "content": text}
    return {"status": "error", "error": "empty_tool_result"}


def _queries(offset: int, size: int) -> list[str]:
    return [QUERY_BANK[(offset + i) % len(QUERY_BANK)] for i in range(size)]


def _issue_owner(payload: dict[str, Any], *, expected_error: bool = False) -> tuple[str, str]:
    if expected_error:
        return "expected_groksearch_validation", "预期的本服务输入校验或取消行为"
    status = payload.get("status")
    if status in {"ok", "partial"}:
        return "ok", "正常返回"

    error = payload.get("error")
    if isinstance(error, str):
        if error in {"invalid_kind", "invalid_params", "queries_empty", "task_not_found"}:
            return "expected_groksearch_validation", f"预期校验错误: {error}"
        return "groksearch_service", f"本服务返回字符串错误: {error}"
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        if code in UPSTREAM_CODES:
            status_code = error.get("upstream_status")
            detail = f"上游错误码 {code}"
            if status_code:
                detail += f", upstream_status={status_code}"
            return "grok2api_upstream", detail
        if code in SERVICE_CODES:
            return "groksearch_service", f"本服务错误码 {code}"
        if code:
            return "chain_or_unknown", f"未知结构化错误码 {code}"
    if status == "error":
        return "chain_or_unknown", "错误结构不足，无法只凭 payload 归属"
    return "ok", "非错误状态"


def _iter_result_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("results"), list):
        items = [item for item in payload["results"] if isinstance(item, dict)]
        return items or [payload]
    result = payload.get("result")
    if isinstance(result, dict):
        if isinstance(result.get("results"), list):
            return [item for item in result["results"] if isinstance(item, dict)]
        return [result]
    return [payload]


def _summarize_payload(
    scenario: str,
    payload: dict[str, Any],
    elapsed_ms: float,
    *,
    expected_error: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(_iter_result_items(payload)):
        owner, reason = _issue_owner(item, expected_error=expected_error)
        rows.append(
            {
                "scenario": scenario,
                "item_index": idx,
                "status": item.get("status") or payload.get("state") or payload.get("status"),
                "owner": owner,
                "owner_reason": reason,
                "elapsed_ms": round(elapsed_ms, 2),
                "session_id": item.get("session_id"),
                "provider": item.get("provider"),
                "model": item.get("model"),
                "sources_count": item.get("sources_count"),
                "content_length": len((item.get("content") or "").strip())
                if isinstance(item.get("content"), str)
                else None,
                "error": item.get("error"),
                "warning": item.get("warning") or payload.get("warning"),
            }
        )
    return rows


async def _call_tool(client: Client, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], float, str]:
    started = time.perf_counter()
    try:
        result = await client.call_tool(name, args)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return _extract_payload(result), elapsed_ms, ""
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "status": "error",
            "error": {
                "code": "mcp_client_exception",
                "message": f"{type(exc).__name__}: {exc}",
            },
        }, elapsed_ms, str(exc)


async def _run_batch_scenario(
    client: Client,
    *,
    name: str,
    queries: list[str],
    timeout: str,
    extra_sources: int,
    jsonl,
) -> list[dict[str, Any]]:
    payload, elapsed_ms, exc = await _call_tool(
        client,
        "web_search_batch",
        {"queries": queries, "timeout": timeout, "extra_sources": extra_sources},
    )
    record = {
        "type": "scenario_payload",
        "scenario": name,
        "elapsed_ms": round(elapsed_ms, 2),
        "exception": exc,
        "payload": payload,
    }
    jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
    jsonl.flush()
    return _summarize_payload(name, payload, elapsed_ms)


async def _run_async_flood(
    client: Client,
    *,
    tasks: int,
    batch_size: int,
    timeout: str,
    extra_sources: int,
    poll_wait: str,
    max_poll_seconds: float,
    jsonl,
) -> list[dict[str, Any]]:
    scenario = f"async_flood_{tasks}x{batch_size}"
    submitted: list[str] = []
    started = time.perf_counter()
    for task_idx in range(tasks):
        payload, elapsed_ms, exc = await _call_tool(
            client,
            "submit_search_task",
            {
                "kind": "web_search_batch",
                "params": {
                    "queries": _queries(task_idx * batch_size, batch_size),
                    "timeout": timeout,
                    "extra_sources": extra_sources,
                },
            },
        )
        jsonl.write(json.dumps({
            "type": "task_submit",
            "scenario": scenario,
            "elapsed_ms": round(elapsed_ms, 2),
            "exception": exc,
            "payload": payload,
        }, ensure_ascii=False) + "\n")
        task_id = payload.get("task_id")
        if isinstance(task_id, str):
            submitted.append(task_id)

    rows: list[dict[str, Any]] = []
    pending = set(submitted)
    while pending and (time.perf_counter() - started) < max_poll_seconds:
        for task_id in list(pending):
            payload, elapsed_ms, exc = await _call_tool(
                client,
                "get_search_task_result",
                {"task_id": task_id, "wait": poll_wait},
            )
            jsonl.write(json.dumps({
                "type": "task_poll",
                "scenario": scenario,
                "task_id": task_id,
                "elapsed_ms": round(elapsed_ms, 2),
                "exception": exc,
                "state": payload.get("state"),
                "payload": payload if payload.get("state") in TERMINAL_STATES else {
                    key: payload.get(key)
                    for key in ("task_id", "kind", "state", "submitted_at", "started_at", "finished_at")
                },
            }, ensure_ascii=False) + "\n")
            jsonl.flush()
            if payload.get("state") in TERMINAL_STATES:
                pending.remove(task_id)
                rows.extend(_summarize_payload(scenario, payload, elapsed_ms))
        if pending:
            await asyncio.sleep(1)

    for task_id in sorted(pending):
        rows.append(
            {
                "scenario": scenario,
                "item_index": -1,
                "status": "running",
                "owner": "chain_or_unknown",
                "owner_reason": f"任务 {task_id} 在 {max_poll_seconds:.0f}s 内未收敛",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "session_id": None,
                "provider": None,
                "model": None,
                "sources_count": None,
                "content_length": None,
                "error": {"code": "task_not_terminal", "task_id": task_id},
                "warning": None,
            }
        )
    return rows


async def _run_expected_errors(client: Client, jsonl) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cases = [
        ("expected_invalid_batch_empty", "web_search_batch", {"queries": ["", "  "]}),
        ("expected_invalid_task_kind", "submit_search_task", {"kind": "bad", "params": {"query": "x"}}),
        ("expected_unknown_task", "get_search_task_result", {"task_id": "task-doesnotexist", "wait": "0s"}),
    ]
    for scenario, tool, args in cases:
        payload, elapsed_ms, exc = await _call_tool(client, tool, args)
        jsonl.write(json.dumps({
            "type": "expected_error",
            "scenario": scenario,
            "elapsed_ms": round(elapsed_ms, 2),
            "exception": exc,
            "payload": payload,
        }, ensure_ascii=False) + "\n")
        rows.extend(_summarize_payload(scenario, payload, elapsed_ms, expected_error=True))
    return rows


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, round((len(values) - 1) * percentile))
    return values[index]


def _write_report(rows: list[dict[str, Any]], output_dir: Path, started_at: str, args: argparse.Namespace) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / f"groksearch-stress-matrix-{started_at}.md"
    total = len(rows)
    owner_counts: dict[str, int] = {}
    scenario_counts: dict[str, dict[str, int]] = {}
    latencies = [float(row["elapsed_ms"]) for row in rows if row.get("elapsed_ms") is not None]
    for row in rows:
        owner = str(row["owner"])
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
        bucket = scenario_counts.setdefault(str(row["scenario"]), {})
        bucket[owner] = bucket.get(owner, 0) + 1

    lines = [
        "# GrokSearch 压测矩阵报告",
        "",
        f"- **started_at_utc**：{started_at}",
        f"- **mcp_url**：{args.mcp_url}",
        f"- **batch_timeout**：{args.batch_timeout}",
        f"- **async_tasks**：{args.async_tasks}",
        f"- **async_batch_size**：{args.async_batch_size}",
        f"- **total_items**：{total}",
        f"- **latency_p50_ms**：{round(statistics.median(latencies), 2) if latencies else 0}",
        f"- **latency_p95_ms**：{round(_percentile(latencies, 0.95), 2)}",
        f"- **latency_max_ms**：{round(max(latencies), 2) if latencies else 0}",
        "",
        "## 问题归属规则",
        "",
        "- `ok`：GrokSearch 结构化返回 `ok/partial`。",
        "- `grok2api_upstream`：GrokSearch 已结构化返回 `upstream_timeout/upstream_network_error/upstream_http_error/upstream_error`，说明请求已进入上游调用路径。",
        "- `groksearch_service`：MCP 工具异常、`internal_error/config_error/invalid_model`、返回结构不一致或服务进程/状态机异常。",
        "- `expected_groksearch_validation`：刻意输入的空 query、非法 task kind、未知 task id 等预期校验行为。",
        "- `chain_or_unknown`：证据不足，不能硬归到任意一侧。",
        "",
        "## 归属汇总",
        "",
        "| owner | count |",
        "|-------|------:|",
    ]
    for owner, count in sorted(owner_counts.items()):
        lines.append(f"| {owner} | {count} |")

    lines.extend(["", "## 场景汇总", "", "| scenario | ok | upstream | service | expected | unknown |", "|----------|---:|---------:|--------:|---------:|--------:|"])
    for scenario, counts in sorted(scenario_counts.items()):
        lines.append(
            "| {scenario} | {ok} | {upstream} | {service} | {expected} | {unknown} |".format(
                scenario=scenario,
                ok=counts.get("ok", 0),
                upstream=counts.get("grok2api_upstream", 0),
                service=counts.get("groksearch_service", 0),
                expected=counts.get("expected_groksearch_validation", 0),
                unknown=counts.get("chain_or_unknown", 0),
            )
        )

    lines.extend(["", "## 非正常样本", ""])
    abnormal = [row for row in rows if row["owner"] not in {"ok", "expected_groksearch_validation"}]
    if not abnormal:
        lines.append("- 无")
    for row in abnormal[:80]:
        lines.append(
            "- `{scenario}` item={item} owner={owner} status={status} reason={reason} error={error}".format(
                scenario=row["scenario"],
                item=row["item_index"],
                owner=row["owner"],
                status=row["status"],
                reason=row["owner_reason"],
                error=json.dumps(row.get("error"), ensure_ascii=False),
            )
        )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


async def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = output_dir / f"groksearch-stress-matrix-{started_at}.jsonl"
    rows: list[dict[str, Any]] = []
    transport = StreamableHttpTransport(args.mcp_url, auth=args.mcp_api_key or None)
    with jsonl_path.open("w", encoding="utf-8") as jsonl:
        async with Client(transport) as client:
            tools = await client.list_tools()
            jsonl.write(json.dumps({
                "type": "tools",
                "tools": sorted(tool.name for tool in tools),
            }, ensure_ascii=False) + "\n")
            rows.extend(await _run_expected_errors(client, jsonl))

            for size in args.batch_sizes:
                rows.extend(await _run_batch_scenario(
                    client,
                    name=f"batch_{size}",
                    queries=_queries(size, size),
                    timeout=args.batch_timeout,
                    extra_sources=args.extra_sources,
                    jsonl=jsonl,
                ))

            rows.extend(await _run_async_flood(
                client,
                tasks=args.async_tasks,
                batch_size=args.async_batch_size,
                timeout=args.batch_timeout,
                extra_sources=args.extra_sources,
                poll_wait=args.poll_wait,
                max_poll_seconds=args.max_poll_seconds,
                jsonl=jsonl,
            ))

    report_path = _write_report(rows, output_dir, started_at, args)
    owner_counts: dict[str, int] = {}
    for row in rows:
        owner = str(row["owner"])
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
    print(f"jsonl={jsonl_path}")
    print(f"report={report_path}")
    print(json.dumps(owner_counts, ensure_ascii=False, sort_keys=True))
    return 0 if owner_counts.get("groksearch_service", 0) == 0 and owner_counts.get("chain_or_unknown", 0) == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--mcp-api-key", default="")
    parser.add_argument("--output-dir", default="docs/development/acceptance")
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[4, 8, 16, 32])
    parser.add_argument("--batch-timeout", default="60s")
    parser.add_argument("--extra-sources", type=int, default=0)
    parser.add_argument("--async-tasks", type=int, default=6)
    parser.add_argument("--async-batch-size", type=int, default=8)
    parser.add_argument("--poll-wait", default="10s")
    parser.add_argument("--max-poll-seconds", type=float, default=900.0)
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
