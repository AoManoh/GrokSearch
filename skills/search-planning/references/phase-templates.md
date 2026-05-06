# Phase Templates

Fill-in JSON templates and one worked example per phase. Schemas mirror the Pydantic models in `src/grok_search/planning.py` — all fields are optional unless marked **required**.

## Phase 1 — Intent

**Template:**
```json
{
  "core_question": "<one-sentence distillation>",
  "query_type": "factual | comparative | exploratory | analytical",
  "time_sensitivity": "realtime | recent | historical | irrelevant",
  "domain": "<optional>",
  "premise_valid": null,
  "unverified_terms": ["<term-1>", "<term-2>"]
}
```

**Required:** `core_question`, `query_type`, `time_sensitivity`.

**Example:**
```json
{
  "core_question": "Which open-source vector databases are most production-ready in 2026 for RAG over millions of documents?",
  "query_type": "comparative",
  "time_sensitivity": "recent",
  "domain": "data engineering",
  "premise_valid": true,
  "unverified_terms": ["CNCF graduated projects", "ANN-benchmark leaders 2026"]
}
```

## Phase 2 — Complexity

**Template:**
```json
{
  "level": 1,
  "estimated_sub_queries": 3,
  "estimated_tool_calls": 5,
  "justification": "<one sentence>"
}
```

`level` ∈ {1, 2, 3}; `estimated_sub_queries` is an int 1..20; `estimated_tool_calls` is an int 1..50.

**Example:**
```json
{
  "level": 3,
  "estimated_sub_queries": 6,
  "estimated_tool_calls": 12,
  "justification": "Cross-comparing 4 candidates × (production usage + perf benchmark + community health) needs ≥6 searches and at least 2 deep-dives."
}
```

## Phase 3 — Sub-query

**Template (one per sub-query — accumulate into a list):**
```json
{
  "id": "sq<N>",
  "goal": "<what this sub-query establishes>",
  "expected_output": "<what success looks like>",
  "boundary": "<what this excludes; must spell out mutual exclusion vs siblings>",
  "depends_on": ["sq<M>"]
}
```

**Required:** `id`, `goal`, `expected_output`, `boundary`.

**Example:**
```json
{
  "id": "sq2",
  "goal": "Quantitative ANN benchmark comparison among Milvus, Qdrant, Weaviate, pgvector",
  "expected_output": "Recall@10 and QPS at 1M, 10M, 100M scale (table)",
  "boundary": "ONLY benchmark numbers; production-readiness signals (community size, breaking changes) belong in sq3",
  "depends_on": ["sq1"]
}
```

## Phase 4 — Search term

**Template (one per term — accumulate):**
```json
{
  "term": "<= 8 words",
  "purpose": "sq<N>",
  "round": 1
}
```

`round` ∈ {1, 2, 3+}: 1 = broad discovery, 2+ = targeted refinement.

Plus one strategy block:
```json
{
  "approach": "broad_first | narrow_first | targeted",
  "fallback_plan": "<optional plain-English fallback>"
}
```

**Example:**
```json
[
  {"term": "Milvus Qdrant Weaviate benchmark 2026", "purpose": "sq2", "round": 1},
  {"term": "pgvector ANN recall billion vectors", "purpose": "sq2", "round": 2}
]
```

## Phase 5 — Tool mapping

**Template (one per sub-query):**
```json
{
  "sub_query_id": "sq<N>",
  "tool": "web_search | web_fetch | web_map",
  "reason": "<one sentence>",
  "params": {}
}
```

**Example:**
```json
{
  "sub_query_id": "sq2",
  "tool": "web_search",
  "reason": "Need synthesised benchmark comparison; primary tables likely live across multiple posts.",
  "params": {"extra_sources": 3}
}
```

## Phase 6 — Execution order

**Template:**
```json
{
  "parallel_groups": [["sq1", "sq2"], ["sq3"]],
  "sequential": ["sq4"],
  "estimated_rounds": 3
}
```

**Example:**
```json
{
  "parallel_groups": [["sq1", "sq3"], ["sq2", "sq4"]],
  "sequential": ["sq5", "sq6"],
  "estimated_rounds": 4
}
```
