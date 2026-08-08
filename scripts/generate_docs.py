"""Generate missing documentation files from code.

Usage: python scripts/generate_docs.py
"""

from pathlib import Path

DOCS_DIR = Path("docs")

# API reference files — use mkdocstrings directives
API_DOCS = {
    "api/secrets.md": """# Secrets API

::: features.secrets
    options:
      show_source: false
      members: [encrypt_json, decrypt_json, is_encrypted_blob]
""",
    "api/importance.md": """# Importance Scoring API

::: shared.importance
    options:
      show_source: false
""",
    "api/memory-types.md": """# Memory Types API

::: shared.memory_types
    options:
      show_source: false
""",
}

# Narrative docs — auto-generated from module docstrings
MODULE_DOCS = {
    "getting-started/installation.md": """# Installation

```bash
pip install mcp-ariel-memory
# or
npm install -g mcp-ariel-memory
```

## Requirements

- Python 3.10+
- SQLite3 (included with Python)
- aiosqlite (auto-installed)

## Docker

```bash
docker pull cipher208/mcp-ariel-memory
docker run -p 8080:8080 cipher208/mcp-ariel-memory
```
""",
    "getting-started/configuration.md": """# Configuration

Configuration is loaded from `config.yaml` in the working directory.

## Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `layers.user.enabled` | true | Enable user memory layer |
| `layers.agent.enabled` | true | Enable agent memory layer |
| `security.rate_limit_per_user` | 100 | Max requests per minute |
| `backup.backup_interval_hours` | 24 | Auto-backup interval |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MCP_MASTER_KEY` | Master key for encryption |
| `BACKUP_CRON_DISABLED` | Disable auto-backups |
""",
    "getting-started/quickstart.md": """# Quick Start

## 1. Install

```bash
pip install mcp-ariel-memory
```

## 2. Configure

```bash
# Set master key
export MCP_MASTER_KEY="your-secret-key"

# Start the server
python -m mcp_server
```

## 3. Use with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ariel-memory": {
      "command": "python",
      "args": ["-m", "mcp_server"]
    }
  }
}
```

## 4. First memory

```
remember(layer="user", user_id="alice", key="name", value="Alice", importance=0.9)
recall(layer="user", user_id="alice", query="name")
```
""",
    "architecture/overview.md": """# Architecture Overview

## Two-Layer Design

- **Layer 1 (User)** — stores facts about users
- **Layer 2 (Agent)** — stores agent identity and decisions

## 4-Level Memory Hierarchy

| Level | Name | Purpose | Retention |
|-------|------|---------|-----------|
| L1 | ReflexBuffer | Recent conversation | 50 messages |
| L2 | SessionStore | Current session | Session duration |
| L3 | EpisodicMemory | Important events | 30 days |
| L4 | CoreMemory | Core facts | Permanent |

## Data Flow

```
Message → L1 (buffer) → ImportanceGate → L2 (session) → EmotionTrigger → L3 (episodic) → L4 (core)
```
""",
    "architecture/layers.md": """# Memory Layers

## L1: ReflexBuffer

Ring buffer storing recent conversation messages. Max 50 entries. FIFO eviction.

## L2: SessionStore

Session-scoped storage. Each session gets its own namespace. Closed when session ends.

## L3: EpisodicMemory

Important events stored with timestamps and tags. Used for emotional context and learning patterns.

## L4: CoreMemory

Permanent key-value store for core facts. Two types: user facts and agent decisions.
""",
    "architecture/connection.md": """# Connection Manager

`AsyncConnectionManager` handles SQLite connections with:

- **Connection pooling** — 64MB cache, WAL mode
- **Thread safety** — each thread gets its own connection
- **Auto-reconnect** — stale connections are reopened automatically
- **Read-only replicas** — optional read-only mode for queries
""",
    "architecture/diagrams.md": """# Architecture Diagrams

```mermaid
graph TD
    A[LLM Agent] -->|MCP Protocol| B[mcp_server]
    B --> C{ImportanceGate}
    C -->|score > 0.3| D[L1: ReflexBuffer]
    C -->|score ≤ 0.3| E[Filtered Out]
    D --> F[L2: SessionStore]
    F --> G{EmotionTrigger?}
    G -->|high emotion| H[L3: EpisodicMemory]
    G -->|normal| I[Consolidation]
    H --> J[L4: CoreMemory]
    I --> J
    B --> K[RAG Engine]
    K --> L[FTS5 Search]
    K --> M[MIB Binary Search]
    K --> N[Hybrid Scoring]
```
""",
    "tools/reference.md": """# MCP Tools Reference

19 unified tools with `layer` parameter (user/agent).

## Memory Operations

| Tool | Description |
|------|-------------|
| `memory_remember` | Store a fact |
| `memory_recall` | Search for facts |
| `memory_forget` | Delete a fact |
| `memory_stats` | Get memory statistics |

## Session Management

| Tool | Description |
|------|-------------|
| `memory_session_start` | Start a new session |
| `memory_session_end` | End session with summary |
| `memory_session_list` | List active sessions |

## Episodes

| Tool | Description |
|------|-------------|
| `memory_episode_save` | Save an episode |
| `memory_episode_recall` | Recall recent episodes |
| `memory_episode_list` | List episodes |
| `memory_episode_get` | Get episode by ID |

## Graph

| Tool | Description |
|------|-------------|
| `memory_graph_add` | Add node to knowledge graph |
| `memory_graph_query` | Query graph by type |
| `memory_graph_nodes` | List all nodes |
| `memory_graph_edges` | List all edges |

## Context

| Tool | Description |
|------|-------------|
| `memory_context` | Get compressed context |
| `memory_context_inject` | Get context for prompt injection |
""",
    "core/reflex.md": """# ReflexBuffer

Ring buffer for recent conversation messages.

```python
from core.reflex import ReflexBuffer

buf = ReflexBuffer(max_size=50)
buf.add(role="user", content="Hello", tokens=5)
recent = buf.get_recent(10)
```

**Invariants:**
- Size never exceeds max_size
- FIFO eviction (oldest removed first)
- Thread-safe for concurrent add/get
""",
    "core/session.md": """# SessionStore

Manages conversation sessions with create/close lifecycle.

```python
from core.session import SessionStore

ss = SessionStore()
session_id = await ss.create_session(user_id)
# ... conversation ...
await ss.close_session(session_id, summary="Discussed architecture")
```
""",
    "core/episodic.md": """# EpisodicMemory

Stores important events with timestamps and tags for emotional context.

```python
from core.episodic import EpisodicMemory

ep = EpisodicMemory()
episode_id = await ep.save(user_id, "Met team lead", 0.8, ["work", "meeting"])
episodes = await ep.search_by_tag(user_id, "work")
```
""",
    "core/memory.md": """# CoreMemory

Permanent key-value store for core facts (L4).

```python
from core.memory import CoreMemory

cm = CoreMemory()
await cm.save(user_id, "name", "Alice", 0.9)
entry = await cm.get(user_id, "name")
results = await cm.search(user_id, "Alice")
await cm.delete(user_id, "name")
```
""",
    "rag/engine.md": """# RAG Engine

Retrieval-Augmented Generation engine with FTS5 + binary embeddings.

```python
from rag.engine import RAGEngine

rag = RAGEngine()
await rag.init_db()
await rag.ingest_text("Title", "Content", user_id="alice")
results = await rag.search("query", user_id="alice")
```

## Strategies

| Strategy | Description |
|----------|-------------|
| `fts` | Full-text search via FTS5 |
| `mib` | Binary embedding search |
| `hybrid` | Combined FTS5 + MIB with RRF |
| `auto` | Automatically selects based on query length |
""",
    "rag/scoring.md": """# Scoring

ITS (Information-Theoretic Scoring) for ranking search results.

Components:
- **Relevance** — FTS5 BM25 + binary similarity
- **Novelty** — surprise based on document frequency
- **Type boost** — bonus for specific content types
""",
    "rag/quantize.md": """# Quantization

Binary embedding quantization for fast similarity search.

```python
from rag.quantize import embed_to_binary, hamming_distance

binary = embed_to_binary(embedding, dim=384)
distance = hamming_distance(binary_a, binary_b)
score = hamming_to_score(distance, dim=384)
```
""",
    "rag/conflict.md": """# Conflict Resolution

Detects conflicting information across memory layers.

```python
from rag.conflict import ConflictResolver

cr = ConflictResolver()
result = await cr.check(user_id, "Python is better than Java")
# result["is_conflict"] = True if conflicting facts exist
```
""",
    "rag/router.md": """# Retrieval Router

Routes queries to appropriate search strategy based on content analysis.

```python
from rag.router import RetrievalRouter

router = RetrievalRouter(user_id="alice")
result = await router.route("How to configure Redis?")
# result.strategy = "fts" or "hybrid"
```
""",
    "features/auth.md": """# Authentication

API key and bearer token authentication.

```python
from features.auth import APIKeyAuth, BearerAuth

auth = APIKeyAuth()
key = auth.create_key("alice", "Production key")
info = auth.verify(key)

ba = BearerAuth()
token = ba.get_token()
assert ba.verify("Bearer " + token)
```
""",
    "features/secrets.md": """# Encryption

Envelope encryption using libsodium secretbox.

```python
from features.secrets import encrypt_json, decrypt_json

blob = encrypt_json({"key": "value"})
data = decrypt_json(blob)
```

Key management: auto-generated or from `MCP_MASTER_KEY` env var.
""",
    "features/backup.md": """# Backup

Automatic and manual backup/restore.

```python
from features.backup import BackupManager

bm = BackupManager()
path = await bm.backup("before-update")
backups = bm.list_backups()
result = await bm.restore("backup-name")
```
""",
    "features/rate-limiting.md": """# Rate Limiting

Per-user rate limiting with SQLite backend.

```python
from features.rate_limiting import RateLimiter

rl = RateLimiter()
result = await rl.check(user_id)
# result["allowed"] = True/False
# result["remaining"] = requests left
```
""",
    "features/compression.md": """# Compression

Memory compression for old data.

```python
from features.compression import MemoryCompressor

mc = MemoryCompressor()
stats = await mc.get_stats(user_id)
```
""",
    "wiki/overview.md": """# Wiki System

Markdown-based wiki with layer separation (user/agent/shared).

## Architecture

- `.md` files on disk = primary storage
- SQLite FTS5 = search index
- 14 content types per layer

## Usage

```python
from wiki import WikiManager

wm = WikiManager(layer="user")
path = await wm.add("diary", "Day 1", "Started project")
results = await wm.search("project")
```
""",
    "wiki/file-wiki.md": """# FileWiki

File-based wiki operations (legacy, now unified in WikiManager).
""",
    "wiki/user-wiki.md": """# User Wiki

User-layer wiki types: diary, relationships, desires, aspirations, work_notes, preferences, retrospective.
""",
    "wiki/agent-wiki.md": """# Agent Wiki

Agent-layer wiki types: decision_log, error_analysis, personality_evolution, emotional_context, wiki_agent, learning_journal, principle_log.
""",
    "hooks/system.md": """# Hooks System

24 hooks for intercepting operations at every stage.

## Registered Hooks

| Hook | Layer | Trigger |
|------|-------|---------|
| `message_received` | Both | New message arrives |
| `emotion_trigger` | Both | Emotion detected |
| `state_delta` | Both | State change |
| `consolidation` | Both | Memory consolidation |
| `error_occurred` | Agent | Error happens |
| `decision_made` | Agent | Decision logged |
| `auto_context` | Both | Context requested |
| `retrieval_router` | Both | Search triggered |

## Usage

```python
from hooks.registry import hook_registry

def my_handler(ctx):
    return {"result": "processed"}

hook_registry.register("message_received", my_handler)
```
""",
    "lifecycle/overview.md": """# Lifecycle

Memory lifecycle management: forgetting, consolidation, emotion triggers.

## Forgetting

Automatic cleanup of old, low-importance data.

## Consolidation

Moving data from L3 (episodic) to L4 (core) based on importance.

## Emotion Triggers

Detecting emotional content and promoting it to episodic memory.
""",
    "operations/deployment.md": """# Deployment

## Docker

```bash
docker build -t mcp-ariel-memory .
docker run -p 8080:8080 -e MCP_MASTER_KEY=secret mcp-ariel-memory
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_MASTER_KEY` | Yes | Master encryption key |
| `BACKUP_CRON_DISABLED` | No | Disable auto-backups |
""",
    "operations/monitoring.md": """# Monitoring

## Dashboard

Real-time HTML dashboard at `http://localhost:8080/dashboard`.

## Metrics

Prometheus-compatible metrics at `http://localhost:8080/metrics`.

## Health Check

`http://localhost:8080/health` — returns server status.
""",
    "operations/testing.md": """# Testing

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=. --cov-report=html

# Property-based tests only
pytest tests/test_hypothesis.py -v
```

250 tests + 79 property-based/logic/chaos tests.
""",
    "contributing.md": """# Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes
4. Run tests: `pytest tests/ -v`
5. Submit a pull request

## Code Style

- Ruff for linting/formatting
- mypy for type checking
- Google docstring style
""",
}


def main():
    created = 0
    for rel_path, content in {**API_DOCS, **MODULE_DOCS}.items():
        full_path = DOCS_DIR / rel_path
        if not full_path.exists():
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            created += 1
        else:
            pass


if __name__ == "__main__":
    main()
