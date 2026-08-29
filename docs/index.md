# a-memory

> Product name of the `mcp-ariel-memory` server — your AI agents forget; a-memory makes them remember.

**4-tier Memory MCP Server for AI agents — layer-isolated, plain SQLite, zero cloud**

[![CI](https://github.com/Cipher208/a-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/Cipher208/a-memory/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-635 passed-brightgreen)](https://github.com/Cipher208/a-memory/actions)
[![Python](https://img.shields.io/badge/python-3.10--3.13-blue)](https://www.python.org/)

---

## What is it?

mcp-ariel-memory is a production-ready MCP server providing persistent, searchable memory for AI agents. It implements a four-layer architecture on a layer-isolated `(layer, user_id, key)` namespace, so user facts and agent identity can never overwrite each other:

- **L1 ReflexBuffer** — short-lived working memory for the current turn
- **L2 SessionStore** — session-scoped state (consolidates into L3/L4 on close)
- **L3 EpisodicMemory** — time-stamped events with emotional weight and tags
- **L4 CoreMemory** — long-lived facts, gated by importance scoring

User facts live in the `user` namespace, agent identity in the `agent` namespace; both share the same schema but separate keys.

## Key Features

| Feature | Description |
|---------|-------------|
| **5 universal primitives** | `think` / `dream` / `forget` / `evolve` / `project`; exposure tiers via `ARIEL_EXPOSE`: `primitives,wiki` adds `wiki_add`/`wiki_search`/`wiki_list`/`wiki_delete`/`wiki_summarize`, `brief` adds `daily_brief`, `all` exposes the full 36-tool surface |
| **4-tier memory** | L1 ReflexBuffer → L2 SessionStore → L3 EpisodicMemory → L4 CoreMemory, layer-isolated `(layer, user_id, key)` namespaces |
| **Typed memory** | 13 categories with per-type retention, decay, and boost |
| **RAG search** | FTS5 + binary embeddings + hybrid RRF ranking, multi-source merge |
| **Knowledge graphs** | Epistemic (facts/decisions, typed nodes + edges) + Temporal (timeline events) |
| **Wiki system** | .md files as source of truth, 15 content types (7 user + 1 `project_spec` + 7 agent), 6 analytical perspectives (`wiki_summarize`), schema lint on save/sync |
| **Context snapshot** | `memory_context_inject` auto-writes a per-layer `CONTEXT.md` (curated context + 6 wiki perspectives + recent episodes) for cross-session agent access |
| **Saga pattern** | Multi-step ops with retry, idempotency, compensation |
| **Envelope encryption** | NaCl `SecretBox` (XSalsa20-Poly1305) via PyNaCl, keychain-first key resolution |
| **Memory scopes** | Per-user isolation on HTTP: an API-key-bound client cannot spoof another user's `user_id` (stdio/local unchanged) |
| **Platform-aware async** | aiosqlite on Linux/macOS, asyncio.to_thread on Windows |
| **SHA-256 dedup** | Prevents duplicate observations within 5-minute window |
| **Circuit breaker** | Prevents cascading LLM/embedding failures |
| **Token budget** | Limits context injection to 2000 tokens |
| **Privacy filter** | Strips API keys, secrets, and private tags |

## Quick Start

=== "pip (recommended)"

    ```bash
    pip install a-memory
    a-memory --transport stdio
    ```

=== "From source"

    ```bash
    git clone https://github.com/Cipher208/a-memory.git
    cd a-memory && uv sync
    uv run ariel-memory
    ```

=== "Docker"

    ```bash
    docker build -t a-memory .
    docker run -p 8000:8000 a-memory
    ```

### Claude Desktop

```json
{
  "mcpServers": {
    "a-memory": {
      "command": "a-memory"
    }
  }
}
```

## Documentation

| Section | Description |
|---------|-------------|
| [Architecture](architecture/overview.md) | Layered model, L1-L4, consolidation, 23 DB tables |
| [MCP Tools](tools/reference.md) | Tool reference (layer ops; agents normally see only the 5 primitives) |
| [RAG & Search](rag/engine.md) | Unified search, BM25 conflict similarity, type-aware boost |
| [Hooks](hooks/system.md) | 19 unique hook names (12 user + 13 agent config slots), importance gating |
| [Operations](operations/deployment.md) | Transports, health, auth, configuration |
| [API Reference](api/secrets.md) | Auto-generated from docstrings |

## Status

- **Version:** 1.8.1
- **Tests:** 635+ passed (including 25 property-based Hypothesis tests)
- **DB tables:** 23 (alembic head `init_v8`)
- **Tool surface:** 5 primitives by default · 10 with `primitives,wiki` · 11 with `primitives,wiki,brief` · 36 with `all`
- **Python:** 3.10–3.13
- **Platform:** Windows, Linux, macOS, Docker
