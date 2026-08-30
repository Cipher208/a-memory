# MCP Tools Reference

Product name: **a-memory** · package: `mcp-ariel-memory` · v1.8.0

a-memory exposes three tool surfaces:

| Surface | Tools | How |
|---------|-------|-----|
| **Primitives** (default) | 5 | What every MCP client sees out of the box |
| **Primitives + wiki** | 9 | `ARIEL_EXPOSE=primitives,wiki` — adds `wiki_add`/`wiki_search`/`wiki_list`/`wiki_delete` for agents that manage wiki pages directly |
| **+ brief** | 10 | `ARIEL_EXPOSE=primitives,wiki,brief` — adds `daily_brief` (one-call status report) |
| **Full surface** | 36 | `ARIEL_EXPOSE=all` restores legacy granular tools |

`think` also accepts optional `wiki_type` / `wiki_title`: passing either forces a
wiki save with an explicit page name instead of the automatic Thought_<ts>
routing, so large notes and curated pages are reachable without extra tools.

All tools accept a `layer` parameter (`user` or `agent`). Layers are fully isolated: separate `(layer, user_id, key)` namespaces in L3/L4, separate wiki spaces and graphs. Agent-layer writes never overwrite user facts.

---

## Universal Primitives (default surface)

### `think`

Universal write primitive: routes a thought to the correct storage based on content size, importance, and emotional weight. Never silently drops content. Auto-classifies each thought as high / medium / low `training_value` (decision + outcome regexes, RU+EN) into its temporal event metadata.

```json
{ "text": "We decided to use SQLite over Postgres — zero-config deployment matters more than concurrency here.", "layer": "auto" }
```

**Parameters**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `text` | string | required | Thought content |
| `layer` | `user\|agent\|auto` | `auto` | `auto` scores agent-voice vs user-fact signals and routes accordingly |
| `user_id` | string | `"default"` | |
| `wiki_type` | string | null | Optional; passing it (or `wiki_title`) forces a wiki save with this explicit type |
| `wiki_title` | string | null | Optional explicit page title instead of the automatic `Thought_<ts>` naming |

**Routing logic**

1. **> 2000 chars** → Wiki page (`decision_log` type for agent layer, `diary` for user), plus a short summary link stored in L4 (importance > 0.7) or L3.
2. **< 60 chars AND importance > 0.7** → L4 CoreMemory fact.
3. **≥ 60 chars OR emotional weight > 0.5** → L3 episodic memory.
4. **Fallback**: anything matched by no rule above goes to L3 — a write always lands somewhere.
5. **Relation detection** (`X is/related to/connected to Y`) additionally creates a knowledge-graph node.

Returns `ThinkResult`: `routing` (importance, length, emotional_weight, resolved_layer) and the list of `actions` taken.

> Side effects: fires `message_received` hook (and `emotion_trigger` on the user layer); significant thoughts also become `thought` events on the temporal timeline.

### `dream`

Universal read primitive: hybrid search across **all** layers (L3 episodes, L4 facts, Wiki, Graph) with context construction and token budgeting. Each call is recorded to `recall_events` for telemetry (separate from the timeline; no `temporal_events` pollution).

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

> When `intent="recent"`, the temporal timeline is prepended to the summary (latest 5 events for the user/agent).

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

> Always writes to the **agent** layer by design — agent identity lives separately from user facts, and `evolve` is the primitive for shaping the former. There is no `layer` parameter; use `think` if you need to record something about the user.

Returns `EvolveResult`: `status`, `summary` (from the hook pipeline). A `personality_shift` event is also recorded on the temporal timeline.

### `project`

Manages project-specific context. Projects are **global** (keyed by name, no user/agent split). Structured data (identity, decisions with outcomes, artifact map, code-symbol index) lives in `projects.db`; large documents go to the Wiki as `project_spec` pages.

```json
{ "action": "recall", "name": "my-app" }
```

**Actions**

| Action | Purpose |
|--------|---------|
| `init` | Create project: Wiki `project_spec` page + identity row in projects.db |
| `update` | Update context page, refresh the code map (runs `graphify` if installed and a `path` is set) |
| `mapping` | Register an artifact: `details` = file path, plus role/status |
| `decision` | Record a decision with rationale (`details`) and `outcome`; logs a `project_decision` event on the temporal timeline |
| `recall` | Full report: status, decisions history, artifacts, code-symbol count |
| `audit` | Dream-style gap analysis: targeted searches per dimension (Architecture / Security / Testing), L4 conflict scan, projects.db completeness verdicts |
| `archive` | Archive the spec page contents to the Shadow Bin and remove the wiki file |

Returns `ProjectResult` (+ `wiki_ref`, `code_map`, decisions/artifacts arrays depending on action).

---

### `memory_hook`

Fires one external lifecycle event into the hook system. This is the **harness transport** (stdio/MCP) for the auto-hooks push model — the agent's harness (or a daemon) calls it on lifecycle moments; both it and `POST /api/hooks/{event}` route through the same dispatcher. Isolation is inherited: each agent instance has its own data dir.

```json
{ "event": "session_ended", "payload": { "summary": "fixed deploy script" }, "layer": "user", "user_id": "default" }
```

**Events** (unknown → `ValueError`)

| Event | Payload keys | What ariel does |
|-------|--------------|-----------------|
| `session_started` | `text`?, `budget`? | Returns the critical inject block (ACT-R top-5 relevant + recent L1 + important facts, token-capped) |
| `session_ended` | `summary` | Persists the session summary to L3 episodic (`session_summary` tag) |
| `new_message` | `text` | `evaluate_importance` heuristic; score ≥ `hooks.auto_save_threshold` (0.5) → L3 + graph node; ≥ 0.8 → also L4 core |
| `auto_save_candidate` | `text` | Same pipeline as `new_message` (explicit candidate from a daemon) |
| `post_context_compression` | `query` | Returns rehydrate candidates via retrieval |
| `context_threshold` | — | Advice: eviction candidates from L1 (decision stays harness-side) |
| `memory_pressure` | — | Advice: L1 ring size + compress hint (decision stays harness-side) |

Returns the fired handlers' results dict.

The reference harness-side client for both surfaces is the bundled
`autohooks` runtime (`python -m autohooks daemon|inject --config <agent>.yaml`):
a per-agent daemon tails the conversation store and fires `new_message`, and
`inject` prints the session-start critical set. See `autohooks/examples/`.

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
| `memory_session_end` | Close a session. Optional `topics` / `state_deltas` feed the deterministic quality score persisted on the row (4 components: depth / decision / linked_entries / user_engagement, 0-80). Fires consolidation hooks. |
| `memory_session_list` | Recent sessions. |

### Knowledge graph

| Tool | Description |
|------|-------------|
| `memory_graph_add` | Add a node (`content`, `node_type`, `tags`); type maps to domain hooks (error/decision/personality/emotion). Social types (`person`, `organization`) dedup per user — re-add returns the existing node. Optional `relates_to` (node_id) + `relation` (`knows`, `works_with`, `family_of`, `friend_of`, `met`, `mentions`) create an edge in the same call. |
| `memory_graph_query` | Query by tag or node type. |
| `memory_graph_nodes` | List nodes (optionally by type), highest confidence first. |
| `memory_graph_edges` | List edges (optionally for one node: `direction` = `out` outgoing (default), `in` backlinks, `both`) with both endpoint contents. |

### Wiki

Wiki types: 8 user (`diary`, `relationships`, `desires`, `aspirations`, `work_notes`, `preferences`, `retrospective` + `project_spec`) and 7 agent (`decision_log`, `error_analysis`, `personality_evolution`, `emotional_context`, `wiki_agent`, `learning_journal`, `principle_log`).

| Tool | Description |
|------|-------------|
| `wiki_add` | Add or update a page (`title`, `content`, `wiki_type`, `tags`). |
| `wiki_search` | Search pages. |
| `wiki_list` | List pages, optionally filtered by type. |
| `wiki_delete` | Delete a page by title. |
| `wiki_summarize` | Return a token-budgeted digest of wiki pages from one of 6 analytical perspectives (practical / epistemic / psychological / social / temporal / metacognitive). See below. |

#### `wiki_summarize` — 6-perspective digest

Each perspective maps to an existing `(layer, wiki_type)` pair — this is **perspective filtering, not new types**. Use it to slice the wiki through one analytical lens without writing a custom search.

```json
{ "perspective": "psychological", "query": "frustration with caching" }
```

**Parameters**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `perspective` | `practical\|epistemic\|psychological\|social\|temporal\|metacognitive` | required | One of 6 curated perspectives |
| `layer` | `user\|agent` | `agent` | Overridden by the perspective's canonical layer; passed through to the wiki binding |
| `query` | string | `""` | Empty = list all pages of the perspective's type; non-empty = FTS5 search filtered by type |
| `limit` | int | `10` | Cap on page count before token truncation |

**Perspective → wiki_type mapping**

| Perspective | Layer | wiki_type |
|---|---|---|
| `practical` | agent | `decision_log` |
| `epistemic` | agent | `learning_journal` |
| `psychological` | agent | `emotional_context` |
| `social` | user | `relationships` |
| `temporal` | user | `retrospective` |
| `metacognitive` | agent | `principle_log` |

Returns a dict with `perspective`, `layer`, `wiki_type`, `pages` (list of `{title, type, tags}`), `count`, `truncated`, and `digest` (markdown summary, ≤ 2000 tokens).

> Auto-gated under `ARIEL_EXPOSE=primitives,wiki` via the `wiki_` prefix — no separate config. 9 of 15 wiki types are not mapped to any perspective; use `wiki_search` for those.

### Operations & maintenance

| Tool | Description |
|------|-------------|
| `memory_stats` | Per-level counts: L1 buffer, L2 sessions, L3 episodes, L4 facts, wiki pages, graph nodes. Also `recall_count` (total `dream` calls) and `avg_session_quality` (0-100). |
| `daily_brief` | One-call daily brief: pending work (L4 todo), recent activity (temporal + recall count), suggested action (todo follow-ups + open sessions). Non-fatal per section; deterministic, no LLM. Exposed via `ARIEL_EXPOSE=primitives,brief` or `all`. |
| `wiki_link` | List or add typed links between wiki pages (`review_of` / `revises` / `follows`). `action="list"` returns a page's in/out links; `action="add"` creates one. Auto-exposed by the `wiki` tier. |
| `memory_context` | Compressed context summary for prompt injection (top-10 facts, recent turns, wiki, episodes). |
| `memory_context_inject` | Same, plus explicit `estimated_tokens`/`was_truncated` against the token budget. Runs an inline L3→L4 consolidation step before reading L4 (returns `consolidated_episodes` and `last_consolidation_ts`). Also writes a 3-section `CONTEXT.md` snapshot to `<MCP_MEMORY_DATA_DIR>/<layer>/CONTEXT.md` (returns `context_md_path` and `perspectives_count`); write failures are non-fatal. |
| `memory_search` | Hybrid search over RAG + Wiki with `strategy` and `sources` selection. |
| `memory_cleanup` | Maintenance sweep: dedup core, compress episodes, clean dream buffer/audit/backup/saga, run forgetting compaction. |
| `memory_lucidity_purge` | Emergency purge of everything newer than N hours (L4, L3, audit log, graph, staging). |
| `memory_watch` | Operator CRUD over `watch_rules` (introspection of the rules ariel's `auto_save_text` already applies — no new behavior). `action="list"` returns `{id, name, trigger, predicate, action, enabled, hits_24h}`; `add` accepts `name, trigger, predicate_json, action_kind`; `disable` and `delete` take `rule_id`. Predicate keys are whitelisted (`min_importance`, `l4_min_importance`, `keywords`, `sender`). |
| `memory_proposals` | Review surface for staged mutations (C1.11): `action="list"` returns pending proposals for `user_id`; `action="decide"` applies (`approve=true`) or rejects (`approve=false`) one proposal by `proposal_id`; `action="revert"` undoes an **applied** proposal with exact provenance (core_write / archive; consolidation not supported). Apply executes the exact write the direct path would have; every decision is audit-logged. Pending proposals also surface in the session-start inject. |
| `memory_report_card` | Operator digest (C1.14): proposal counts by status + last decisions, auto-save tier sums (dispatched / saved_l3 / saved_l4 / saved_graph), open gaps (via `compute_session_gaps`), dream-marker count — over `period_hours` (default 24). Hidden unless `ARIEL_EXPOSE` includes the `review` tier. |
| `memory_recall_protocol` | Multi-axis /recall protocol (D1.1): `markers → session → semantic → expand → day` blocks, deduped, budget-capped (`budget=2000`). Proportional — empty `query` = zero-state (markers + day only); with a query all five axes fire. Operator tier (hidden by default); the same engine drives the Hermes prefetch surface via `autohooks recall`. Tool count 41 → 42 (43 after D2.1's `wiki_read`). |
| `wiki_read` | Read a wiki page's full content by `path` (D2.1) — the progressive-disclosure read leg (`wiki_list`/`wiki_search` → `wiki_read`). Skills live under `wiki_type="skill"` as plain Markdown; `skill` is a first-class wiki type and lint caps skill pages at 4KB. Tool count 42 → 43. |
| `memory_skill_promote` | Store pipeline (D2.2): promote distilled memory into a skill page. `episode_ids` → promote existing episodes verbatim (idempotent via the `skill_promoted` tag; provenance footer added); `title`+`content` → write an agent-distilled skill directly. The nightly hook's 4th phase (`skill_promotion`) auto-promotes fresh `dream_skill` episodes the same way. No LLM in ariel — dream_skill episodes are agent-distilled at write time (C1.12); raw auto_save chatter needs harness-side distillation (v2 ceiling). Tool count 43 → 44. |
| `memory_get_smart_context` | Weighted token distribution (D1.10): every source (important 30% / relevant 30% / recent 15% / day 15% / ops 10%) gets a weight-proportional floor first; leftover redistributes with a 2x-floor ceiling — a fat source cannot starve the rest (unlike the sequential inject builder). Returns `blocks` + per-source `allocations`. Tool count 44 → 45. |
| `memory_reflect` | Reflection system (D1.16): deterministic meta-memories. `action="write"` computes a reflection over `period_hours` (episode counts, recurring topics — no LLM) and stores it in the `reflections` table; `action="list"` reads recent reflections back (optional `topic` filter). The nightly hook's 5th phase writes the daily reflection automatically. Migration `20260830_1000_d116`. Tool count 45 → 46. |
| `memory_scratchpad` | Agent scratchpad (D1.15, L2.5): working memory between session and episodes. `action="write"` (key+content, upsert; cap 20 entries, oldest evicted) / `"read"` / `"clear"` / `"promote"` (move agent-judged-useful entries into L3 episode or L4 fact, drop from pad — the agent is the distiller). Entries re-inject at session start as the `scratchpad` block. Tool count 46 → 47. |
| `memory_quality` | Memory quality metrics (D1.19): was_useful → score feedback loop. `action="feedback"` (entry_id + useful): useful → `recall_useful` audit row (feeds ACT-R frequency, D1.17) + importance +0.05 (cap 1.0); not useful → −0.05 (floor 0.05); every adjustment importance_audit-logged (`agent_feedback`). `action="report"` aggregates per-entry useful counts with current importance. Tool count 47 → 48. |
| `memory_backup` | Backup management: `status` \| `now` \| `list` \| `restore`. |
| `memory_saga` | Run compensation sagas (`consolidate` \| `backup`) with auto-rollback. |
| `memory_data` | Per-user export/import of memory data. |
| `memory_sync_replica` | Sync the read-only replica used by the dashboard/metrics. |
| `memory_api_key` | API-key management for HTTP transport: `list` \| `create` \| `revoke`. |

---

## Surface selection

```bash
# default — primitives only (no ARIEL_EXPOSE)
a-memory                         # stdio transport
a-memory --transport http --port 8000 --dashboard

# primitives + wiki tools
ARIEL_EXPOSE=primitives,wiki a-memory

# primitives + wiki + daily brief
ARIEL_EXPOSE=primitives,wiki,brief a-memory

# full legacy surface
ARIEL_EXPOSE=all a-memory
```

The gate lives in `mcp_server/server.py` (`PRIMITIVE_TOOLS`); hidden tools remain reachable through the primitives themselves and the dashboard HTTP surface.
