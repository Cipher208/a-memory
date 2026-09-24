---
type: "Reference"
title: "Architecture overview"
openwiki_generated: true
verified:
  - by: openwiki/0.6.0
    at: 2026-09-24T20:32:03.830Z
sources:
  - id: openwiki-source-92ddeafbf5685f53a3443811
    resource: repo://embeddings_service.py
  - id: openwiki-source-cf7a691b6dbb3f5cf84511a3
    resource: repo://mcp_server/server.py
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-3f7704ec313aac599d89f75b
    resource: repo://shared/embeddings.py
  - id: openwiki-source-b104f925fdb75091bb99d98d
    resource: repo://shared/memory_types.py
generated: { by: "opencode", at: "2026-09-24T20:32:03.830Z" }
---

# Architecture overview

a-memory is a 4-tier agent memory system with hybrid search and a knowledge
graph — stored entirely in plain SQLite files. Zero cloud, zero external
APIs: one directory holds the whole memory and backs up with `cp`.

## The four tiers

1. **User layer** — facts about users.
2. **Agent layer** — agent identity, decisions, errors, personality.
3. **L3 episodes** — session-scoped event streams, later absorbed by
   consolidation passes.
4. **L4 canon** — long-term declarable kinds (`L4_DECLARABLE_KINDS` in
   `shared/memory_types.py`): preference, commitment, rule, instruction,
   relationship, decision.

User and agent layers never share a namespace — isolation is structural, not
a filter.

## Retrieval

Hybrid search combines SQLite FTS5 full-text with a memory-index backend,
plus vector similarity through a shared local e5 embeddings service
(`embeddings_service.py`, loopback `:8710`). A knowledge graph over
memorised items is maintained by miners and enrichment passes in
`lifecycle/` with temporal and epistemic structure in `graph/`.

## Access

The only supported access path is the stdio MCP server (`mcp_server/`,
FastMCP), with tool visibility scoped by `ARIEL_EXPOSE`. See
`architecture/mcp-server.md`.

## Maintenance

Memory is event-driven: hook handlers (`hooks/`), a poll daemon
(`autohooks/`), and lifecycle passes for consolidation, distillation,
forgetting, and graph repair (`lifecycle/`). See `architecture/lifecycle-hooks.md`
and `architecture/memory-layers.md`.
