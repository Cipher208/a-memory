# Changelog

Full history lives in [CHANGELOG.md](https://github.com/Cipher208/a-memory/blob/master/CHANGELOG.md).

## v1.9.0 (2026-08-29)

### Highlights
- **Memory Scopes (D1.13)** — per-user isolation on HTTP: an API-key-bound client cannot spoof another user's `user_id`; stdio/local unchanged
- **Self-building graph (B1.2 + B1.3)** — `person`/`organization` entities with per-user dedup and `relates_to`/`relation` edges on `memory_graph_add`; nightly `graph_builder` extracts people and relationships from episodes (RU+EN patterns); graph backlinks via `memory_graph_edges(direction=...)`
- **ACT-R activation + CLS replay (D1.17 + D1.18)** — recall frequency + recency boost retrieval scoring; `dream` confirms used facts (`recall_useful`), nightly replay boosts confirmed L4 facts
- **GraphRAG (B1.6)** — retrieval chain vector/FTS → graph expand → rerank; 1-hop neighbors of graph hits join results with damped scores
- **Lineage + transitions (B1.4 + B1.5)** — promoted facts record parent summaries (`get_lineage`); every memory move (staging→l4, l4→archived, …) validated and persisted to `memory_transitions` with metrics
- **Causal memory (B1.7)** — `record_causal` action→outcome links with strength in the epistemic graph
- **SQLite quick wins (A2.6–A2.9)** — PRAGMA set completed (page_size/auto_vacuum), bulk `executemany` imports, 8 composite indexes for hot queries

See the repo CHANGELOG for the complete list.

## v1.8.1 (2026-08-27)

### Highlights
- **6 analytical wiki perspectives** — `wiki_summarize` filters the wiki through a curated mapping to existing `wiki_type` (practical / epistemic / psychological / social / temporal / metacognitive)
- **Wiki schema lint** — 6 checks on save/sync; `missing_index` auto-fixable
- **Dream-cycle inject + CONTEXT.md snapshot** — `memory_context_inject` curates L3→L4 inline, then writes a 3-section per-layer `CONTEXT.md`
- **Session quality scoring** — 5-component deterministic score (0-100) persisted on session close, exposed via `memory_stats.avg_session_quality`
- **Recall telemetry** — every `dream` recorded in `recall_events`; `memory_stats.recall_count`; feeds the `recall_usage` score component
- **training_value classification** — `think` tags each thought high/medium/low (decision + outcome regexes, RU+EN)
- **Daily brief tool** — `daily_brief` one-call 3-section report; new `brief` exposure tier
- **Tool count 36** — default 5-primitive surface unchanged; wiki + brief tiers opt-in via `ARIEL_EXPOSE`

See the repo CHANGELOG for the complete list.

## v1.8.0 (2026-08-26)

### Highlights
- **Temporal timeline wired end-to-end** — think/evolve/project events + dream `recent` digest
- **Tiered tool exposure** — `ARIEL_EXPOSE=primitives,wiki` (and `all`)
- **Export/import payload v1.1** — sessions round-trip, newest-wins conflict guard
- **Optional `embeddings` extra** — sentence-transformers, CPU-pinned
- **End-to-end pipeline test suite** — real stores, 25 property-based tests

See the repo CHANGELOG for the complete list.

## v1.7.0 (2026-08-24)

### Highlights
- **mcp 2.x native** (`mcp.server.mcpserver.MCPServer`), `mcp[cli]>=2,<3`
- **5 universal primitives by default** (`think`/`dream`/`forget`/`evolve`/`project`); full surface via `ARIEL_EXPOSE=all`
- **Layer registry** — one-call addition of new memory layers
- **Project memory layer** — decisions, artifact map, graphify code index in a separate `projects.db`
- **Dream staging pipeline** restored and wired into the hourly consolidation sweep
- **DB maintenance loop** — size thresholds, metrics, auto-VACUUM
- **Layer isolation** for core_memory/episodes (alembic migration)
- Config drift warnings for per-agent configs; ~15 previously dead yaml keys now live

See the repo CHANGELOG for the complete list.
