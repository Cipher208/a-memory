# Architecture Overview

## Layered Model

```
┌─────────────────────────────────────────────┐
│              MCP Client (LLM Agent)          │
├─────────────────────────────────────────────┤
│      mcp_server (MCPServer, mcp 2.x)        │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │ Tools Layer  │  │    Hooks Pipeline    │  │
│  │ (56 tools,   │  │ (19 hooks, gating)   │  │
│  │ 6 exposed)   │  │                      │  │
│  └──────┬───────┘  └──────────┬───────────┘  │
│         │                     │              │
│  ┌──────▼─────────────────────▼───────────┐  │
│  │         Unified Memory Layer            │  │
│  │  L1: ReflexBuffer (ring, 50 entries)   │  │
│  │  L2: SessionStore (sessions)           │  │
│  │  L3: EpisodicMemory (episodes)         │  │
│  │  L4: CoreMemory (typed key-value)      │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ RAG      │ │ Wiki     │ │ Graphs       │ │
│  │ Engine   │ │ (FTS5)   │ │ (epistemic + │ │
│  │          │ │          │ │  temporal)    │ │
│  └──────────┘ └──────────┘ └──────────────┘ │
└─────────────────────────────────────────────┘
```

Agents see the six primitives (`think` / `dream` / `forget` / `evolve` / `project` / `memory_hook`) by default; coherent opt-in tiers via `ARIEL_EXPOSE` add the rest — `context` (recall protocol, recap, smart context, steering, compression), `insight` (query DSL, fact blame, quality, reflections, stats, raw searches), `write` (remember, typed saves, rules engine, scratchpad, counterfactuals, episodes, sessions), plus `wiki` / `brief` / `review`. Recommended live value: `ARIEL_EXPOSE=primitives,context,insight,write,wiki,brief,review` (46 tools); `ARIEL_EXPOSE=all` restores the full 56-tool surface incl. admin ops.

## Memory Layers

| Layer | Class | Purpose | Max Size |
|-------|-------|---------|----------|
| L1 | ReflexBuffer | Recent messages (ring buffer) | 50 |
| L2 | SessionStore | Sessions with summaries | recent sessions |
| L3 | EpisodicMemory | Episodes with emotional weight and tags | grows (consolidated hourly) |
| L4 | CoreMemory | Long-term typed facts (key-value) | persistent |

User and agent layers are isolated: separate `(layer, user_id, key)` namespaces in L3/L4, separate wiki spaces and graphs.

## Consolidation

1. **Writes** (`think`) route by importance/emotion/size directly into L4 facts, L3 episodes, Wiki pages, or graph nodes.
2. **Reads** (`dream`) stage their digests into DreamBuffer staging.
3. The **hourly sweep** drains staging through per-user consolidation, deduplicates episodes per layer, then promotes recurring episodes toward core facts; staging leftovers older than 24h are dropped.
4. **DB self-maintenance** follows consolidation: size warnings, Prometheus gauge, auto-VACUUM when thresholds are met.

## Database

Single SQLite file (WAL mode) with 23 domain tables:

- `core_memory`, `episodes`, `sessions`, `staging_memories` — memory layers + consolidation staging
- `rag_chunks`, `rag_pages`, `rag_relations` — RAG search index
- `user_wiki` / `agent_wiki` (+ FTS5 shadows) — Wiki pages per layer
- `epi_nodes`, `epi_edges`, `epi_tags` — epistemic knowledge graph
- `temporal_events`, `temporal_links` — timeline graph (layer-scoped); records thought / personality-shift / project-decision events from the primitives, surfaced by `dream(intent="recent")` as a Timeline digest
- `archived_memories` — Shadow Bin for soft-deleted content
- `audit_log`, `importance_audit`, `memory_conflicts` — observability
- `rate_limits`, `embedding_cache`, `saga_step_log`, `memory_kind_registry` — infrastructure
- `ner_cache` — entity-extraction cache, created on demand by `graph_miners._ensure_ner_cache()`
  (like `co_pairs_cache`, not a migration). Keyed by `(text_hash, tag)` where the tag fingerprints
  the synonym dictionary and the NER backend, so a dictionary edit or model upgrade invalidates it
  instead of serving stale entities.

Both content-hash caches start empty, and the nightly enrich of a layer has a per-layer budget
(`NIGHTLY_LAYER_BUDGET_S`, 180 s since 06.10; 120 s before), so a base whose layer has never been
cached cannot fill them in one pass. `scripts/warm_embedding_cache.py` and `scripts/warm_ner_cache.py`
fill `embedding_cache` and `ner_cache` for one live base outside that budget, for exactly the keys the
miners will look up.
Both are cache-only: they write no edges, roles or anomaly tags, so warming changes no graph state —
verify that by comparing `epi_nodes`/`epi_edges`/`epi_tags` counts before and after.

## Nightly Schedule

One cron thread (`features/backup_cron.BackupCron`, a tick a minute) drives two independent schedules:

- **Backup** — every `backup_interval_hours` (24 h) plus jitter, under a per-base `flock` so the
  gateway/dashboard twin processes cannot each write a full backup.
- **Nightly pass** — gated by `features.cycles.nightly_gate`: the cycle is due 24 h after the last
  *successful* pass, subject to the cost cap. It is deliberately NOT part of the backup branch. Until
  06.10 it was reachable only from inside it, and `_do_backup` stamps `_last_backup` *before* the
  hooks run, so a pass that died — on the layer budget, or on a restart in the middle of it — waited a
  full day for its next attempt, leaving no trace that it had been tried. Two consecutive passes were
  lost that way on the live hermes base, while both days still wrote a backup.

A failed pass now backs off rather than waiting for the next backup: 15 min → 1 h → 4 h, persisted in
`.backup_cron_state.json` so it survives the frequent restarts here. A layer call that runs past
`NIGHTLY_LAYER_BUDGET_S` leaves its coroutine RUNNING on the shared main loop — the timeout abandons,
it does not cancel — so an unfinished job refuses a new pass until it completes, rather than letting
two passes occupy the loop at once; a pass still unfinished after `NIGHTLY_INFLIGHT_GRACE_S` is
treated as lost so retries can never be blocked forever. `cycles_state.json`'s `last_nightly` is
written only after both layers finish, so an interrupted pass still leaves the cycle due.

## Debugging a Running Server

`kill -USR1 <pid>` appends every thread's Python stack to
`<data_dir>/logs/stack-dump.txt` (`mcp_server/server.py:_install_faulthandler`).
The handler runs in C, so it still reports while the event loop is blocked —
which is the case that matters, because a nightly layer call that runs past its
budget leaves its coroutine RUNNING on the shared main loop (the timeout
abandons, it does not cancel).

Use `scripts/dump_stacks.sh [hermes|mimocode|cowagent]` rather than signalling a
pid you found by hand. It resolves the server by its own `MCP_MEMORY_DATA_DIR`
and requires a python interpreter as the first cmdline field, because
mimocode's launcher is `sh -c '... python3 .../mcp_server/server.py'` — a shell
that also carries the env var and also matches "mcp_server".

**It checks `/proc/<pid>/status:SigCgt` before signalling, and this is not
optional.** SIGUSR1's default disposition is to terminate, so a server running
code older than `_install_faulthandler` would be killed by the command meant to
inspect it — which is what happened to all three servers on 2026-10-06
14:21:28 CEST. Bit 9 of `SigCgt` is set exactly when the handler is installed; if
it is clear the script refuses and says the service needs a restart. Never
replace that check with a post-hoc "did the dump grow" test.

py-spy is the better tool when you have root — with `ptrace_scope=1` it cannot
attach otherwise. It is not installed; `uvx py-spy dump --pid <pid>` fetches it
on demand. Note faulthandler prints bare `Thread 0x<id>` headers and never
thread names, so the `backup-cron` thread name only shows up in a py-spy dump.

## Platform-Aware Async

- **Linux/macOS**: aiosqlite (true async SQLite)
- **Windows**: sync sqlite3 + `asyncio.to_thread()` (event loop never blocks)

Both paths use WAL mode, busy_timeout=5000, and 64MB page cache.
