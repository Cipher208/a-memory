# mcp-ariel-memory

**Universal Two-Layer Memory MCP Server for AI agents**

[![CI](https://github.com/Cipher208/mcp-ariel-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/Cipher208/mcp-ariel-memory/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-372 passed-brightgreen)](https://github.com/Cipher208/mcp-ariel-memory/actions)
[![Python](https://img.shields.io/badge/python-3.10--3.13-blue)](https://www.python.org/)

---

## What is it?

mcp-ariel-memory is a production-ready MCP server providing persistent, searchable memory for AI agents. It implements a two-layer architecture:

- **Layer 1 (User)** — facts about users: preferences, conversation history, emotional context
- **Layer 2 (Agent)** — agent identity: decisions, errors, personality evolution

## Key Features

| Feature | Description |
|---------|-------------|
| **25 MCP tools** | Unified layer-based API (`user`/`agent` parameter) |
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

=== "npm (recommended)"

    ```bash
    npx mcp-ariel-memory --transport stdio
    ```

=== "pip"

    ```bash
    pip install git+https://github.com/Cipher208/mcp-ariel-memory.git
    python -m mcp_server --transport stdio
    ```

=== "Docker"

    ```bash
    docker build -t ariel-memory .
    docker run -p 8000:8000 ariel-memory
    ```

### Claude Desktop

```json
{
  "mcpServers": {
    "ariel-memory": {
      "command": "npx",
      "args": ["mcp-ariel-memory", "--transport", "stdio"]
    }
  }
}
```

## Documentation

| Section | Description |
|---------|-------------|
| [Architecture](architecture/overview.md) | Two-layer model, L1-L4, consolidation, 22 DB tables |
| [MCP Tools](tools/reference.md) | All 19 tools with parameters and examples |
| [RAG & Search](rag/engine.md) | Unified search, BM25 conflict similarity, type-aware boost |
| [Hooks](hooks/system.md) | 24 hooks (12 user + 12 agent), type-aware gating |
| [Operations](operations/deployment.md) | Transports, health, auth, configuration |
| [API Reference](api/secrets.md) | Auto-generated from docstrings |

## Status

- **Version:** 1.5.0
- **Tests:** 400+ passed (including 25 property-based Hypothesis tests)
- **DB tables:** 23
- **Python:** 3.10–3.13
- **Platform:** Windows, Linux, macOS, Docker
