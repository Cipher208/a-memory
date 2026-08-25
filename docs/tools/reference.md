# MCP Tools Reference

Product name: **a-memory** · package: `mcp-ariel-memory` · v1.7.0

a-memory exposes two tool surfaces:

| Surface | Tools | How |
|---------|-------|-----|
| **Primitives** (default) | 5 | What every MCP client sees out of the box |
| **Full surface** | 35 | Set `ARIEL_EXPOSE=all` to restore legacy granular tools |

All tools accept a `layer` parameter (`user` or `agent`). Layers are fully isolated: separate `(layer, user_id, key)` namespaces in L3/L4, separate wiki spaces and graphs. Agent-layer writes never overwrite user facts.

---

## Universal Primitives (default surface)

### `think`

Universal write primitive: routes a thought to the correct storage based on content size, importance, and emotional weight. Never silently drops content.

```json
{ "text": "We decided to use SQLite over Postgres — zero-config deployment matters more than concurrency here.", "layer": "auto" }
```

**Parameters**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `text` | string | required | Thought content |
| `layer` | `user\|agent\|auto` | `auto` | `auto` scores agent-voice vs user-fact signals and routes accordingly |
| `user_id` | string | `"default"` | |

**Routing logic**

1. **> 2000 chars** → Wiki page (`decision_log` type for agent layer, `diary` for user), plus a short summary link stored in L4 (importance > 0.7) or L3.
2. **< 60 chars AND importance > 0.7** → L4 CoreMemory fact.
3. **≥ 60 chars OR emotional weight > 0.5** → L3 episodic memory.
4. **Fallback**: anything matched by no rule above goes to L3 — a write always lands somewhere.
5. **Relation detection** (`X is/related to/connected to Y`) additionally creates a knowledge-graph node.

Returns `ThinkResult`: `routing` (importance, length, emotional_weight, resolved_layer) and the list of `actions` taken.

### `dream`

Universal read primitive: hybrid search across **all** layers (L3 episodes, L4 facts, Wiki, Graph) with context construction and token budgeting.

```json
{ "query": "database architecture", "intent": "core", "limit": 10 }
```

**Parameters**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `query` | string | required | Search query |
| `limit` | int\|null | `null` | Resolved through the RAG chain when omitted |
| `layer` | `user\|agent` | `"user"` | |
| `user_id` | string | `"default"` | |
| `intent` | `recent\|core\|balanced` | `"balanced"` | Weight bias of the ranking |

Returns `DreamResult`: `summary` (markdown sections per hit, truncated to the token budget), `truncated`, `result_count`.

### `forget`

Context-aware forgetting with Shadow Bin (soft-delete archive) support.

```json
{ "key": "old project config", "scope": "fuzzy", "shadow_bin": true }
```

**Parameters**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `key` | string | required | Key or search pattern |
| `scope` | `exact\|fuzzy\|recent` | `"exact"` | `exact`: one L4 key · `fuzzy`: pattern across L4/L3/Graph · `recent`: time-based mass purge |
| `minutes` | int | `60` | Window for `recent` scope |
| `shadow_bin` | bool | `true` | Archive before deleting (skipped for `recent`) |

Returns `ForgetResult`: `deleted_l4`, `deleted_l3`, `deleted_graph` counts.

### `evolve`

Updates agent personality: stores the instruction in agent CoreMemory (importance 1.0) and fires the `personality_shift` evolution hook.

```json
{ "instruction": "Be more assertive about security issues in code review." }
```

Returns `EvolveResult`: `status`, `summary` (from the hook pipeline).

### `project`

Manages project-specific context. Projects are **global** (keyed by name, no user/agent split). Structured data (identity, decisions with outcomes, artifact map, code-symbol index) lives in `projects.db`; large documents go to the Wiki as `project_spec` pages.

```json
{ "action": "recall", "name": "my-app" }
```

**Actions**

| Action | Purpose |
|--------|---------|
| `init` | Create project: Wiki `project_spec` page + identity row in projects.db |
| `update` | Update context page, refresh the code map (via optional `graphify` integration) |
| `mapping` | Register an artifact: `details` = file path, plus role/status |
| `decision` | Record a decision with rationale (`details`) and `outcome` |
| `recall` | Full report: status, decisions history, artifacts, code-symbol count |
| `audit` | Dream-style gap analysis: targeted searches per dimension (Architecture / Security / Testing), L4 conflict scan, projects.db completeness verdicts |
| `archive` | Move the Wiki spec page to the archive |

Returns `ProjectResult` (+ `wiki_ref`, `code_map`, decisions/artifacts arrays depending on action).

---

## Full surface (`ARIEL_EXPOSE=all`)

Legacy granular operations behind the primitives. Grouped by domain.

### Core memory (L4)

| Tool | Description |
|------|-------------|
| `memory_remember` | Save a fact (key/value/importance). Strips secrets, deduplicates within a session TTL, runs the importance-gate hook. Also writes a graph node. |
| `memory_recall` | Search across L3 + L4 with TTL-cached results. |
| `memory_forget` | Delete one L4 fact by key. |

### Episodic memory (L3)

| Tool | Description |
|------|-------------|
| `memory_episode_save` | Save an episode (`summary`, emotional `weight`, `tags`). |
| `memory_episode_recall` | Recall episodes, optionally filtered by tag. |
| `memory_episode_list` | Paginated episode listing (`limit`/`offset`). |
| `memory_episode_get` | Fetch one episode by ID. |

### Working sessions (L2)

| Tool | Description |
|------|-------------|
| `memory_session_start` | Open a session. |
| `memory_session_end` | Close with summary; fires consolidation hooks. |
| `memory_session_list` | Recent sessions. |

### Knowledge graph

| Tool | Description |
|------|-------------|
| `memory_graph_add` | Add a node (`content`, `node_type`, `tags`); type maps to domain hooks (error/decision/personality/emotion). |
| `memory_graph_query` | Query by tag or node type. |
| `memory_graph_nodes` | List nodes (optionally by type), highest confidence first. |
| `memory_graph_edges` | List edges (optionally outgoing from one node) with both endpoint contents. |

### Wiki

Wiki types: 7 user + 7 agent (incl. `project_spec` for user layer).

| Tool | Description |
|------|-------------|
| `wiki_add` | Add or update a page (`title`, `content`, `wiki_type`, `tags`). |
| `wiki_search` | Search pages. |
| `wiki_list` | List pages, optionally filtered by type. |
| `wiki_delete` | Delete a page by title. |

### Operations & maintenance

| Tool | Description |
|------|-------------|
| `memory_stats` | Per-level counts: L1 buffer, L2 sessions, L3 episodes, L4 facts, wiki pages, graph nodes. |
| `memory_context` | Compressed context summary for prompt injection (top-10 facts, recent turns, wiki, episodes). |
| `memory_context_inject` | Same, plus explicit `estimated_tokens`/`was_truncated` against the token budget. |
| `memory_search` | Hybrid search over RAG + Wiki with `strategy` and `sources` selection. |
| `memory_cleanup` | Maintenance sweep: dedup core, compress episodes, clean dream buffer/audit/backup/saga, run forgetting compaction. |
| `memory_lucidity_purge` | Emergency purge of everything newer than N hours (L4, L3, audit log, graph, staging). |
| `memory_backup` | Backup management: `status` \| `now` \| `list` \| `restore`. |
| `memory_saga` | Run compensation sagas (`consolidate` \| `backup`) with auto-rollback. |
| `memory_data` | Per-user export/import of memory data. |
| `memory_sync_replica` | Sync the read-only replica used by the dashboard/metrics. |
| `memory_api_key` | API-key management for HTTP transport: `list` \| `create` \| `revoke`. |

---

## Surface selection

```bash
# default — primitives only
uvx a-memory

# full legacy surface
ARIEL_EXPOSE=all uvx a-memory
```

The gate lives in `mcp_server/server.py` (`PRIMITIVE_TOOLS`); hidden tools remain reachable through the primitives themselves and the dashboard HTTP surface.
