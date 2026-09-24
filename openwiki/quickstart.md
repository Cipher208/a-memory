---
type: "Reference"
title: "Quickstart"
openwiki_generated: true
verified:
  - by: openwiki/0.6.0
    at: 2026-09-24T20:32:03.830Z
sources:
  - id: openwiki-source-83c9ecd3284b33afe13167b9
    resource: repo://config.yaml
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
generated: { by: "opencode", at: "2026-09-24T20:32:03.830Z" }
---

# Quickstart

## Install

```bash
pip install a-memory
a-memory          # MCP server on stdio — connect from any MCP client
```

Optional extras: `a-memory[embeddings]` for real multilingual embeddings.
From source: `git clone https://github.com/Cipher208/a-memory.git`,
then `uv sync` and `uv run ariel-memory`.

Point any MCP client at it:

```json
{
  "mcpServers": {
    "a-memory": {
      "command": "a-memory"
    }
  }
}
```

HTTP transport with dashboard: `a-memory --transport http --port 8000
--dashboard`.

## Minimal config

`config.yaml` is the single source of truth (every key is read by code —
see `docs/CONTROL_MAP.md`). Layers can be toggled at the API boundary:

```yaml
layers:
  user:
    enabled: true
  agent:
    enabled: true
```

Per-agent override: copy the file and point the agent's MCP environment at
it via `MCP_CONFIG_PATH`. Tier limits (`l1_buffer_size`, `l2_session_limit`,
`l3_episodic_limit`, `l4_core_limit`) live under `limits:`.

## First round-trip

1. `think` a fact — it is routed to the right layer automatically.
2. `dream` a query — hybrid retrieval returns matching memories.
3. `project init` + `project decision` — track per-project context so the
   next session picks up where this one left off.

## Where data lives

One directory holds the entire memory as plain SQLite files (back up with
`cp`). Never mix runtimes: every MCP server gets its own dedicated venv.
