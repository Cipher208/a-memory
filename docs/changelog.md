# Changelog

Full history lives in [CHANGELOG.md](https://github.com/Cipher208/a-memory/blob/master/CHANGELOG.md).

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
