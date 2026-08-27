# Architecture Overview

## Layered Model

```
┌─────────────────────────────────────────────┐
│              MCP Client (LLM Agent)          │
├─────────────────────────────────────────────┤
│      mcp_server (MCPServer, mcp 2.x)        │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │ Tools Layer  │  │    Hooks Pipeline    │  │
│  │ (35 tools,   │  │ (19 hooks, gating)   │  │
│  │ 5 exposed)   │  │                      │  │
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

Agents see the five primitives (`think` / `dream` / `forget` / `evolve` / `project`) by default; `ARIEL_EXPOSE=primitives,wiki` adds the five wiki tools, and `ARIEL_EXPOSE=all` restores the full 35-tool surface.

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

## Platform-Aware Async

- **Linux/macOS**: aiosqlite (true async SQLite)
- **Windows**: sync sqlite3 + `asyncio.to_thread()` (event loop never blocks)

Both paths use WAL mode, busy_timeout=5000, and 64MB page cache.
