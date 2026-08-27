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

## External wiki directories

Wiki pages can be auto-imported from any directory of `.md` files on disk. Each layer (`user` / `agent`) accepts its own list of external roots. The server re-scans them on a timer (default: every 30 minutes) and indexes new or changed files into FTS5. Type is auto-detected from the file path or content.

```yaml
wiki:
  user:
    external_dirs:
      - "~/Documents/notes"
      - "~/work/drafts"
    diary: true
    relationships: true
    # ... other wiki types as needed
  agent:
    external_dirs: []
    decision_log: true
    # ...

backup:
  wiki_sync_interval_minutes: 30   # how often to re-scan external_dirs
```

**Behavior**

- On `sync_external` tick, every `**/*.md` under each path is checked (sha256 of content).
- New files → imported; changed files (content differs) → re-imported; identical files → skipped.
- A file's `wiki_type` is guessed from the path (`diary/2026-09.md` → `diary`) or the content's first 200 chars (fallback: `diary` for user, `wiki_agent` for agent).
- Files are copied into the layer's wiki folder under the resolved type, e.g. `~/.mcp-ariel-memory/wiki/user/diary/2026-09.md`. The original file is left untouched.
- The external root is **read-only** from a-memory's perspective — edits there are picked up on the next sync, not synchronously.

**Cadence** is controlled by `backup.wiki_sync_interval_minutes`. To force an immediate rescan, restart the server (sync runs at startup AND on the timer). There is no on-demand `wiki_sync` tool or filesystem watcher in v1.8.0 — for live sync, drop the cadence to a low value (e.g. `1`).

**Path format**: tilde (`~`) is expanded; relative paths are resolved against the data dir (`MCP_MEMORY_DATA_DIR`). Symlinks are followed. The directory must be readable by the a-memory process; missing directories are silently skipped (with a debug log).

**Limitations**: v1.8.0 has no inotify / file-watcher hook. Cadence-only.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_MASTER_KEY` | auto-generated | Master key for envelope encryption |
| `MCP_MEMORY_DATA_DIR` | `~/.mcp-ariel-memory` | Data directory for SQLite databases |
| `MCP_CONFIG_PATH` | repo-root `config.yaml` | Per-agent config file path |
| `ARIEL_EXPOSE` | `primitives` | Tool exposure tier: `primitives,wiki` adds `wiki_add`/`wiki_search`/`wiki_list`/`wiki_delete`/`wiki_summarize`; `all` restores the full 35-tool surface |
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
