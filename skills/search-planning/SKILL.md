---
name: search-planning
description: Plan a multi-step web research task before executing searches - distill intent, score complexity (1-3), decompose into mutually-exclusive sub-queries, draft search terms (max 8 words each), map sub-queries to tools (web_search / web_fetch / web_map), and define an execution order with parallel groups and sequential dependencies. Use whenever a question is comparative, exploratory, or analytical, or whenever it likely requires more than one search.
---

# search-planning

Plan a multi-step web research task before executing searches. Produces an executable plan: distilled intent, complexity score, mutually-exclusive sub-queries, search terms, tool mappings, and an execution order with parallel groups + sequential dependencies.

## When to use

- The question is comparative, exploratory, or analytical (not a single-fact lookup).
- You expect to run more than one search.
- Multiple unknown external classifications (e.g. "CCF-A conferences", "OWASP Top 10") need verification before deeper search.

## When *not* to use

- Single-search factual questions ("when did X release?") — just call `grok-web-search` directly.
- The user wants a quick scan, not a structured plan.

## Workflow

This skill is **process knowledge only** — no scripts, no state files. Fill the templates inline in your context. After producing the plan, execute the steps with `grok-web-search` / `tavily-web-fetch` / `tavily-web-map`.

```
Phase 1 → 2 → 3 → 4 → 5 → 6
intent  complexity  sub-queries  search-terms  tool-mapping  execution-order
                ↓
        L1 → stop after Phase 3
        L2 → stop after Phase 5
        L3 → run all 6
```

### Phase 1 — Intent

Distil the user's question into structured fields. **Required output:**

- `core_question` (one sentence)
- `query_type` ∈ {`factual`, `comparative`, `exploratory`, `analytical`}
- `time_sensitivity` ∈ {`realtime`, `recent`, `historical`, `irrelevant`}

**Optional output (when applicable):**

- `domain` — e.g. `cybersecurity`, `data engineering`
- `premise_valid` — set `false` if the question contains a flawed assumption
- `unverified_terms` — external classifications/rankings the AI's training data may misrepresent (each becomes a prerequisite Phase-3 sub-query)

### Phase 2 — Complexity

Score complexity on a 1–3 scale. The score determines which subsequent phases are required.

| Level | Heuristic | Required phases |
|---|---|---|
| 1 | 1–2 searches expected | 1, 2, 3 |
| 2 | 3–5 searches expected | 1, 2, 3, 4, 5 |
| 3 | ≥ 6 searches *or* needs cross-source corroboration | all 6 |

Output: `level`, `estimated_sub_queries`, `estimated_tool_calls`, `justification`.

### Phase 3 — Sub-queries

Decompose the core question into independent sub-queries. **Each sub-query must:**

- Have a unique `id` (e.g. `sq1`, `sq2`)
- State a `goal` and `expected_output`
- Define a `boundary` that names the **mutual exclusion** vs sibling sub-queries (not just the broader domain)
- List `depends_on` (other sub-query ids that must complete first), or empty

A bad boundary is "research X" — an *informative* boundary is "history of X (excludes current state, which is sq2)".

### Phase 4 — Search terms

For each sub-query, draft 1+ search terms that you'll feed into `grok-web-search`'s `--query`.

- Each term ≤ **8 words**.
- One term per sub-query id (don't combine `sq1+sq2` into one query).
- Tag each term with a `round`: `1` = broad discovery, `2+` = targeted refinement after seeing round-1 results.
- Drop redundant synonyms (use `RAG` not `RAG retrieval augmented generation`).

Pick an overall `approach`: `broad_first` / `narrow_first` / `targeted` (known-item).

### Phase 5 — Tool mapping

For each sub-query id, choose one of `web_search` / `web_fetch` / `web_map` and a one-line `reason`.

- `web_search` (`grok-web-search`): default for any "what / who / why / how" sub-query.
- `web_fetch` (`tavily-web-fetch`): when the URL is known and full content is needed.
- `web_map` (`tavily-web-map`): when you need to discover the URL surface area before deciding what to fetch.

### Phase 6 — Execution order

Group sub-queries that can run concurrently into `parallel_groups`; list those that must wait into `sequential` (with `depends_on` pointers respected).

- `parallel_groups`: list of lists of sub-query ids — each inner list runs in one concurrent batch.
- `sequential`: ids that must run after a specific dependency completes.
- `estimated_rounds`: count of execution rounds (= max length over `parallel_groups` chains).

## Templates and rules

For fill-in JSON templates and worked examples, see [`references/phase-templates.md`](references/phase-templates.md). For complexity rubric, boundary anti-patterns, and parallelism heuristics, see [`references/decision-rules.md`](references/decision-rules.md).

## Hand-off

The output of this skill is a Markdown plan (or JSON, if you choose). The next step is to execute it — typically by issuing `grok-web-search` / `tavily-web-fetch` / `tavily-web-map` calls in the order Phase 6 specifies. Each of those skills is independently useful — this planning skill is also useful as a stand-alone framework if you don't have the others installed.
