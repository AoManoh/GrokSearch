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


QUERY_BANK: list[dict[str, str]] = [
    {"category": "zh_regression", "query": "AIGC 最新行业应用和监管趋势"},
    {"category": "zh_regression", "query": "论文降重 合规方法和学术诚信要求"},
    {"category": "zh_fresh", "query": "今天人工智能芯片市场最新动态"},
    {"category": "zh_fresh", "query": "最近大模型开源项目有哪些重要更新"},
    {"category": "zh_academic", "query": "检索扩展生成 RAG 最新综述论文"},
    {"category": "zh_academic", "query": "多模态大模型评测基准 最新研究"},
    {"category": "zh_technical", "query": "FastAPI 生产环境部署最佳实践 2026"},
    {"category": "zh_technical", "query": "PostgreSQL 向量检索 pgvector 性能优化"},
    {"category": "zh_business", "query": "中国新能源汽车出口 近期数据"},
    {"category": "zh_business", "query": "跨境电商独立站 最新增长策略"},
    {"category": "zh_policy", "query": "欧盟人工智能法案 最新实施进展"},
    {"category": "zh_policy", "query": "中国生成式人工智能服务管理 暂行办法 解读"},
    {"category": "zh_health", "query": "睡眠质量改善 最新科学建议"},
    {"category": "zh_health", "query": "运动后恢复 蛋白质摄入 最新指南"},
    {"category": "zh_edge", "query": "小众开源项目 llama.cpp Vulkan 后端进展"},
    {"category": "zh_edge", "query": "冷门城市低空经济试点 最新消息"},
    {"category": "en_fresh", "query": "latest updates from xAI this week"},
    {"category": "en_fresh", "query": "current state of AI agent benchmarks"},
    {"category": "en_academic", "query": "recent papers on retrieval augmented generation evaluation"},
    {"category": "en_academic", "query": "state space models versus transformers recent research"},
    {"category": "en_technical", "query": "Kubernetes Gateway API production best practices"},
    {"category": "en_technical", "query": "Python 3.13 performance improvements summary"},
    {"category": "en_business", "query": "NVIDIA latest earnings AI datacenter revenue"},
    {"category": "en_business", "query": "global EV battery market trends latest"},
    {"category": "en_policy", "query": "EU AI Act implementation timeline latest"},
    {"category": "en_policy", "query": "US AI safety regulation latest executive actions"},
    {"category": "en_health", "query": "latest evidence on intermittent fasting health outcomes"},
    {"category": "en_health", "query": "WHO latest guidance on air pollution health risks"},
    {"category": "en_edge", "query": "WebGPU support status in major browsers"},
    {"category": "en_edge", "query": "SQLite vector search extensions comparison"},
    {"category": "mixed", "query": "AI 搜索 search grounding citations 最新实践"},
    {"category": "mixed", "query": "Claude Code Windsurf Cascade MCP server best practices"},
    {"category": "platform", "query": "GitHub trending agentic coding tools"},
    {"category": "platform", "query": "Reddit discussions about Grok API reliability"},
    {"category": "platform", "query": "Twitter X latest xAI Grok release notes"},
    {"category": "finance", "query": "Bitcoin ETF inflows latest weekly data"},
    {"category": "finance", "query": "Federal Reserve latest interest rate decision"},
    {"category": "science", "query": "latest James Webb Space Telescope discoveries"},
    {"category": "science", "query": "fusion energy latest experimental milestones"},
    {"category": "security", "query": "latest critical CVEs in Linux kernel"},
    {"category": "security", "query": "supply chain security SLSA latest guidance"},
    {"category": "developer", "query": "Next.js latest release breaking changes"},
    {"category": "developer", "query": "React compiler production adoption status"},
    {"category": "developer", "query": "Django latest LTS security updates"},
    {"category": "developer", "query": "Rust async ecosystem latest comparison"},
    {"category": "education", "query": "AI tutors education latest evidence"},
    {"category": "education", "query": "academic integrity AI writing detection limitations"},
    {"category": "climate", "query": "latest IPCC climate mitigation report findings"},
    {"category": "climate", "query": "global renewable energy capacity latest statistics"},
    {"category": "localization", "query": "日本 生成AI ガイドライン 最新"},
    {"category": "localization", "query": "한국 AI 반도체 최신 동향"},
]


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
            return {"status": "unknown", "content": text, "sources_count": 0}
    return {"status": "error", "content": "empty tool result", "sources_count": 0}


def _build_rounds(rounds: int) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for idx in range(rounds):
        base = QUERY_BANK[idx % len(QUERY_BANK)]
        selected.append({"round": str(idx + 1), "category": base["category"], "query": base["query"]})
    return selected


async def _run_one(client: Client, item: dict[str, str], extra_sources: int) -> dict[str, Any]:
    started = time.perf_counter()
    payload: dict[str, Any]
    error = ""
    try:
        result = await client.call_tool(
            "web_search",
            {"query": item["query"], "extra_sources": extra_sources},
        )
        payload = _extract_payload(result)
    except Exception as exc:
        payload = {"status": "error", "content": "", "sources_count": 0}
        error = str(exc)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    content = payload.get("content") if isinstance(payload.get("content"), str) else ""
    sources_count = payload.get("sources_count") if isinstance(payload.get("sources_count"), int) else 0
    status = payload.get("status") if isinstance(payload.get("status"), str) else "unknown"
    return {
        "round": int(item["round"]),
        "category": item["category"],
        "query": item["query"],
        "status": status,
        "ok": status == "ok" and bool(content.strip()),
        "partial": status == "partial" and sources_count > 0,
        "content_length": len(content.strip()),
        "sources_count": sources_count,
        "provider": payload.get("provider"),
        "model": payload.get("model"),
        "session_id": payload.get("session_id"),
        "elapsed_ms": elapsed_ms,
        "error": error or payload.get("error"),
        "warning": payload.get("warning"),
    }


def _write_report(records: list[dict[str, Any]], output_dir: Path, started_at: str) -> Path:
    total = len(records)
    ok_count = sum(1 for item in records if item["ok"])
    partial_count = sum(1 for item in records if item["partial"])
    failed_count = total - ok_count - partial_count
    latencies = [float(item["elapsed_ms"]) for item in records]
    providers = sorted({str(item.get("provider") or "unknown") for item in records})
    categories: dict[str, dict[str, int]] = {}
    for item in records:
        bucket = categories.setdefault(item["category"], {"total": 0, "ok": 0, "partial": 0, "failed": 0})
        bucket["total"] += 1
        if item["ok"]:
            bucket["ok"] += 1
        elif item["partial"]:
            bucket["partial"] += 1
        else:
            bucket["failed"] += 1

    report = output_dir / f"groksearch-acceptance-{started_at}.md"
    lines = [
        "# GrokSearch 100 轮服务验收报告",
        "",
        f"- **started_at**：{started_at}",
        f"- **total**：{total}",
        f"- **ok**：{ok_count}",
        f"- **partial**：{partial_count}",
        f"- **failed**：{failed_count}",
        f"- **providers**：{', '.join(providers)}",
        f"- **latency_p50_ms**：{round(statistics.median(latencies), 2) if latencies else 0}",
        f"- **latency_max_ms**：{round(max(latencies), 2) if latencies else 0}",
        "",
        "## 分类结果",
        "",
        "| category | total | ok | partial | failed |",
        "|----------|-------|----|---------|--------|",
    ]
    for category, bucket in sorted(categories.items()):
        lines.append(f"| {category} | {bucket['total']} | {bucket['ok']} | {bucket['partial']} | {bucket['failed']} |")
    lines.extend(["", "## 失败样本", ""])
    for item in records:
        if item["ok"] or item["partial"]:
            continue
        lines.append(f"- **round {item['round']}**：{item['query']} status={item['status']} error={item['error']}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


async def run_acceptance(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = output_dir / f"groksearch-acceptance-{started_at}.jsonl"
    transport = StreamableHttpTransport(args.mcp_url, auth=args.mcp_api_key or None)
    records: list[dict[str, Any]] = []
    async with Client(transport) as client:
        for item in _build_rounds(args.rounds):
            record = await _run_one(client, item, args.extra_sources)
            records.append(record)
            with jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps(record, ensure_ascii=False), flush=True)
    report_path = _write_report(records, output_dir, started_at)
    accepted = [item for item in records if item["ok"] or item["partial"]]
    acceptance_rate = len(accepted) / len(records) if records else 0
    print(f"jsonl={jsonl_path}")
    print(f"report={report_path}")
    print(f"acceptance_rate={acceptance_rate:.2%}")
    if args.rounds < 100:
        return 2
    return 0 if acceptance_rate >= args.min_acceptance_rate else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--mcp-api-key", default="")
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--extra-sources", type=int, default=0)
    parser.add_argument("--min-acceptance-rate", type=float, default=0.95)
    parser.add_argument("--output-dir", default="docs/development/acceptance")
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(run_acceptance(parse_args())))


if __name__ == "__main__":
    main()
