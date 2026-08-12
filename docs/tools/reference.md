# MCP Tools Reference (v1.6.4)

All tools accept a `layer` parameter (`user` or `agent`) to target the appropriate memory layer.

## Universal Primitives (High-Level Cognition)

These tools represent the core cognitive functions of the Ariel-Memory system.

### memory_think
Routes thoughts to correct memory layers based on content size, importance, and emotional weight.

**Logic:**
- **Wiki Save:** If text > 2000 chars, saves to Wiki (`decision_log` for agent, `diary` for user).
- **L4 CoreMemory:** If text < 60 chars and importance > 0.7.
- **L3 EpisodicMemory:** If text >= 60 chars or emotional weight > 0.5.
- **Graph Auto-Expansion:** Detects relationships (e.g., "A is related to B") and adds nodes to the Knowledge Graph.

```json
{
  "layer": "user",
  "text": "The new architecture uses PostgreSQL for persistence. It is connected to the staging buffer.",
  "user_id": "default"
}
```

### memory_dream
Performs a powerful hybrid search across ALL layers (L3, L4, Wiki, Graph) with automatic context construction and token budgeting.

**Parameters:**
- `query`: Search query.
- `intent`: weight bias (`recent`, `core`, `balanced`).
- `limit`: result count.

**Returns:** `DreamResult` with `summary` (truncated to `DEFAULT_TOKEN_BUDGET`), `truncated` flag, and `result_count`.

```json
{
  "layer": "user",
  "query": "database architecture",
  "intent": "core",
  "limit": 10
}
```

### memory_forget
Context-aware forgetting with Shadow Bin (soft-delete) support.

**Parameters:**
- `key`: Target key or search pattern.
- `scope`: `exact` (specific key), `fuzzy` (pattern search), `recent` (time-based).
- `minutes`: For `recent` scope, how far back to purge.
- `shadow_bin`: If true, archives deleted items before removal.

**Returns:** `ForgetResult` (counts of `deleted_l4`, `deleted_l3`, `deleted_graph`).

```json
{
  "layer": "user",
  "key": "old project config",
  "scope": "fuzzy",
  "shadow_bin": true
}
```

## Layer Operations (Standard CRUD)

### memory_remember
Store a specific key-value fact to long-term memory (L4).

```json
{
  "layer": "user",
  "key": "favorite_color",
  "value": "deep purple",
  "importance": 0.8
}
```

### memory_recall
Search memories across L3 (episodes) and L4 (facts).

```json
{
  "layer": "user",
  "query": "preferences",
  "limit": 5
}
```

### memory_stats
Get detailed metrics for the specified layer.

```json
{
  "layer": "agent"
}
```

## Advanced Operations

### memory_evolve
Updates agent personality and triggers behavioral evolution hooks.

```json
{
  "instruction": "Be more assertive and focused on security in code reviews.",
  "user_id": "default"
}
```

### memory_project
Managing project-specific context, file mapping, and gap analysis.

**Actions:** `init`, `update`, `archive`, `mapping`, `audit`.

```json
{
  "action": "audit",
  "name": "mcp-ariel-memory",
  "layer": "agent"
}
```

### memory_backup_create
Manual trigger for the Saga-based backup system.
