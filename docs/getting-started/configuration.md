# Configuration

## Config File

Location: `config.yaml` (project root or `~/.mcp-ariel-memory/config.yaml`)

```yaml
# Layer limits
limits:
  l1_buffer_size: 50       # ReflexBuffer ring buffer
  l2_session_limit: 100    # SessionStore sessions
  l3_episodic_limit: 1000  # EpisodicMemory episodes
  l4_core_limit: 5000      # CoreMemory facts

# RAG settings
rag:
  chunk_size: 500
  chunk_overlap: 100
  search_limit: 10         # default when callers pass limit=None

# Binary embeddings (MIB)
binary:
  mode: naive
  dim: 384

# DB self-maintenance thresholds (checked by the hourly sweep)
storage:
  db_warn_mb: 50
  db_alert_mb: 200
  vacuum_min_mb: 10
  vacuum_freelist_ratio: 0.25

# Dashboard / metrics endpoints
dashboard:
  enabled: false
  port: 8000

# Hooks — single source of truth; a missing hook key means enabled.
# Toggles only: the importance threshold itself is adaptive (EMA), not config.
hooks:
  user:
    importance_gate: true
    consolidation: true
  agent:
    personality_shift: true
    error_occurred: true
```

The repo-root `config.yaml` is the shared default; per-agent copies live outside the repo and are selected with `MCP_CONFIG_PATH`. On startup a-memory warns when such a copy is missing keys that exist in the newer default. See `docs/CONTROL_MAP.md` for the full key-to-subsystem map.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_MASTER_KEY` | auto-generated | Master key for envelope encryption |
| `MCP_MEMORY_DATA_DIR` | `~/.mcp-ariel-memory` | Data directory for SQLite databases |
| `MCP_CONFIG_PATH` | repo-root `config.yaml` | Per-agent config file path |
| `ARIEL_EXPOSE` | `primitives` | `all` restores the full 35-tool surface |
| `MCP_AUTH_TOKEN` | auto-generated | Bearer token for HTTP transport |
| `MCP_AUTH_DISABLED` | unset | Set to `1` to disable auth (`--no-auth` does this) |
| `BACKUP_CRON_DISABLED` | false | Disable backup cron daemon |

## Key Resolution Order

Master key is resolved in this order:

1. **OS keychain** (keyring library) — recommended for production
2. **.env file** in the data dir (`MCP_MASTER_KEY=...`)
3. **`crypto.master_key_hex` in config.yaml**
4. **Environment variable** (`MCP_MASTER_KEY`, argon2id KDF)
5. **Auto-generate** — creates key and saves it

## Transports

### stdio (default)

```bash
python -m mcp_server --transport stdio
```

### HTTP (Streamable)

```bash
python -m mcp_server --transport http --port 8000
```

### With auth

```bash
python -m mcp_server --transport http --auth
```
