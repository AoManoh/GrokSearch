# Decision Rules

## Complexity grading

Score Phase 2 by *both* heuristics; if they disagree, use the higher.

| Heuristic | L1 | L2 | L3 |
|---|---|---|---|
| Search count | 1–2 | 3–5 | ≥ 6 |
| Citation depth | 1 source per claim is fine | 2+ sources per claim recommended | Multiple cross-checked sources required |
| Output shape | One paragraph or table cell | Multi-section answer | Long-form report with comparative table |

A question with `unverified_terms` (Phase 1) is automatically ≥ L2 because each unknown term becomes a prerequisite Phase-3 sub-query.

## Sub-query boundary anti-patterns

| Anti-pattern | Why it fails | Rewrite |
|---|---|---|
| `boundary: "research vector databases"` | restates the domain, doesn't separate from siblings | `"installation/configuration of Milvus only; benchmarks belong to sq2"` |
| `boundary: "background and current state"` | overlaps with sq covering "current state" | pick one — push the other to its own sub-query |
| `boundary: "anything related to RAG"` | unbounded | constrain by axis (time, technique, vendor, region) |

A good boundary names *what this sub-query refuses to answer*, ideally pointing at the sibling that handles it.

## When to add round 2+

Extend a sub-query into round-2 search terms when round 1 reveals **any of**:

- An unknown vocabulary term used by multiple sources (search for the definition)
- Two sources contradicting each other (search for an authoritative tiebreaker)
- A single canonical source everyone references but you haven't fetched (use `tavily-web-fetch` instead of another `web_search`)

Stop at round 2 unless you find another such trigger; round-3+ is rare.

## Parallel vs sequential

Sub-query A can go in the same `parallel_group` as B iff:

- `A.depends_on` does **not** contain `B.id` (and vice versa)
- They don't compete for the same scarce API quota at the round's expected concurrency

Anything in `depends_on` chains (A → B → C) goes in `sequential`.

## API-quota-aware batching

When `extra_sources > 0`, parallel Tavily/Firecrawl Search calls cost extra quota per parallel-group member. Cap `parallel_groups` size at 3 unless you have headroom verified.

## Tool-choice quick rules

| Symptom | Tool |
|---|---|
| "I need an answer that synthesises across the web" | `web_search` (`grok-web-search`) |
| "I have the URL, give me the page" | `web_fetch` (`tavily-web-fetch`) |
| "I need to find URLs first" | `web_map` (`tavily-web-map`) |
| Time-sensitive ("latest", "今天") | `web_search` — script auto-injects current time |
| Behind a paywall or JS-rendered SPA | Skip `tavily-web-fetch`; try `web_search` (Grok may have it indexed) |
