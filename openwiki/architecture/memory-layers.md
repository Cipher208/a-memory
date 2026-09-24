---
type: "Reference"
title: "Memory layers and primitives"
openwiki_generated: true
verified:
  - by: openwiki/0.6.0
    at: 2026-09-24T20:32:03.830Z
sources:
  - id: openwiki-source-6df6f2d2fac12031ef65289c
    resource: repo://core/episodic.py
  - id: openwiki-source-59ec413dbf7026de5a2fc287
    resource: repo://core/memory.py
  - id: openwiki-source-a741d5ac7c984c15891cca12
    resource: repo://core/session.py
  - id: openwiki-source-bdc2baa12b74cc7662119bae
    resource: repo://mcp_server/tools/primitives/routing.py
  - id: openwiki-source-b80db679a570090482dc0e53
    resource: repo://mcp_server/tools/primitives/think.py
  - id: openwiki-source-042d1fbd4d10897689b0bc9e
    resource: repo://shared/connection.py
  - id: openwiki-source-3f7704ec313aac599d89f75b
    resource: repo://shared/embeddings.py
  - id: openwiki-source-b104f925fdb75091bb99d98d
    resource: repo://shared/memory_types.py
generated: { by: "opencode", at: "2026-09-24T20:32:03.830Z" }
---

# Memory layers and primitives

Storage is split into isolated layers that never share a namespace, with a
small set of primitives as the write path and a larger feature set for
retrieval and maintenance.

## Layers

- **User layer** — facts about users. **Agent layer** — agent identity,
  decisions, errors, personality. The two never share a namespace.
- **L3 episodes** — session-scoped event streams that consolidation later
  absorbs (see `lifecycle/`).
- **L4 canon** — declarable long-term kinds enumerated in
  `shared/memory_types.py` as `L4_DECLARABLE_KINDS`: preference, commitment,
  rule, instruction, relationship, decision.

## Domain core (`core/`)

- `core/memory.py` — central memory domain logic; `core/episodic.py` —
  episode handling; `core/session.py` — session state; `core/projects.py` —
  project-scoped memory; `core/reflex.py` — reflexive processing.

## Primitives (write path)

- The five MCP primitives (`mcp_server/tools/primitives/`) are the canonical
  write path: `think` routes a thought to the right layer, `dream` runs
  hybrid retrieval, `forget` removes with Shadow Bin support, `evolve`
  updates personality, `project` manages project context.

## Feature modules (`features/`)

- Retrieval and maintenance capabilities: `recall.py`, `inject.py`,
  `compression.py`, `replay.py`, `rehydrate.py`, `continuity.py`,
  `import_export.py`, `query_dsl.py`, plus quality, audit, and hardening
  modules (`quality.py`, `semantic_audit.py`, `blame.py`, `rate_limiting.py`).

## Shared infrastructure (`shared/`)

- `connection.py` (SQLite access), `embeddings.py` (client for the shared e5
  service), `crypto.py` + `master_key.py`, `middleware.py`, `migrations.py`,
  `dream_buffer.py`, `tokens.py`, `textcat.py`.
