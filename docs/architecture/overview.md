# Architecture Overview

## Two-Layer Model

```
┌─────────────────────────────────────────────┐
│              MCP Client (LLM Agent)          │
├─────────────────────────────────────────────┤
│         mcp_server (FastMCP)                 │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │ Tools Layer  │  │    Hooks Pipeline    │  │
│  │ (19 tools)   │  │ (24 hooks, gating)  │  │
│  └──────┬───────┘  └──────────┬───────────┘  │
│         │                     │              │
│  ┌──────▼─────────────────────▼───────────┐  │
│  │         Unified Memory Layer            │  │
│  │  L1: ReflexBuffer (ring, 50 entries)   │  │
│  │  L2: EpisodicMemory (sessions)         │  │
│  │  L3: SessionStore (entries)            │  │
│  │  L4: CoreMemory (key-value, 5000)      │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ RAG      │ │ Wiki     │ │ Graphs       │ │
│  │ Engine   │ │ (FTS5)   │ │ (epistemic + │ │
│  │          │ │          │ │  temporal)    │ │
│  └──────────┘ └──────────┘ └──────────────┘ │
└─────────────────────────────────────────────┘
```

## Memory Layers

| Layer | Class | Purpose | Max Size |
|-------|-------|---------|----------|
| L1 | ReflexBuffer | Recent messages (ring buffer) | 50 |
| L2 | EpisodicMemory | Session-level summaries | 100 sessions |
| L3 | SessionStore | Conversation entries | 500 entries |
| L4 | CoreMemory | Long-term facts (key-value) | 5000 facts |

## Consolidation

Data flows from L1 → L2 → L3 → L4 via consolidation:

1. **L1 → L2**: ReflexBuffer overflow triggers session summary
2. **L2 → L3**: Episodic entries are stored as searchable entries
3. **L3 → L4**: Important facts are promoted to core memory
4. **L4**: Long-term storage with typed retention policies

## Database

Single `memory.db` file with ~23 tables:

- `memory_entries` — L3 session entries
- `core_facts` — L4 key-value store
- `episodic_sessions` — L2 session summaries
- `rag_chunks` — RAG search index
- `wiki_index` — Wiki metadata index
- `wiki_fts` — Wiki full-text search (FTS5)
- `saga_state` — Saga persistence
- `importance_audit` — Importance scoring logs
- `epistemic_nodes/edges` — Knowledge graph
- `temporal_events/links` — Timeline graph
- And more...

## Platform-Aware Async

- **Linux/macOS**: aiosqlite (true async SQLite)
- **Windows**: sync sqlite3 + `asyncio.to_thread()` (event loop never blocks)

Both paths use WAL mode, busy_timeout=5000, and 64MB page cache.
