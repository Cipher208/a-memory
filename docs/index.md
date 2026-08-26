# a-memory

> Product name of the `mcp-ariel-memory` server — your AI agents forget; a-memory makes them remember.

**Universal Two-Layer Memory MCP Server for AI agents**

[![CI](https://github.com/Cipher208/a-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/Cipher208/a-memory/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-372 passed-brightgreen)](https://github.com/Cipher208/a-memory/actions)
[![Python](https://img.shields.io/badge/python-3.10--3.13-blue)](https://www.python.org/)

---

## What is it?

mcp-ariel-memory is a production-ready MCP server providing persistent, searchable memory for AI agents. It implements a two-layer architecture:

- **Layer 1 (User)** — facts about users: preferences, conversation history, emotional context
- **Layer 2 (Agent)** — agent identity: decisions, errors, personality evolution

## Key Features

| Feature | Description |
|---------|-------------|
| **5 universal primitives** | `think` / `dream` / `forget` / `evolve` / `project`; exposure tiers via `ARIEL_EXPOSE`: `primitives,wiki` adds wiki_add/search/list/delete, `all` exposes the full 35-tool surface |
| **4-layer memory** | L1 ReflexBuffer → L2 Episodic → L3 Session → L4 Core |
| **Typed memory** | 13 categories with per-type retention, decay, and boost |
| **RAG search** | FTS5 + binary embeddings + hybrid scoring |
| **Knowledge graphs** | Epistemic (facts/decisions) + Temporal (timeline) |
| **Wiki system** | .md files as source of truth, 14 content types |
| **Saga pattern** | Multi-step ops with retry, idempotency, compensation |
| **Envelope encryption** | libsodium secretbox, keychain-first key resolution |
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
| [Hooks](hooks/system.md) | 19 lifecycle hooks (12 user + 13 agent slots), importance gating |
| [Operations](operations/deployment.md) | Transports, health, auth, configuration |
| [API Reference](api/secrets.md) | Auto-generated from docstrings |

## Status

- **Version:** 1.8.0
- **Tests:** 400+ passed (including 25 property-based Hypothesis tests)
- **DB tables:** 23
- **Python:** 3.10–3.13
- **Platform:** Windows, Linux, macOS, Docker
