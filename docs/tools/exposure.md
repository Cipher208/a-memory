# Tool Exposure — slots, tiers, presets (Stage 2-C manifest)

Product: **a-memory** · package: `mcp-ariel-memory` · v1.11.0+

Three mechanisms control what an MCP client sees:

1. **Primitives** — always-visible scene verbs: `think`, `dream`, `forget`,
   `evolve`, `project`, `memory_hook`, `wake_up`.
2. **Tiers** (`ARIEL_EXPOSE` comma list) — flat tool groups. Every tool is in
   at least one tier (orphans = 0, enforced by
   `tests/test_mcp/test_exposure_invariants.py`).
3. **Meta layer** (`ARIEL_META=1`) — one dispatcher tool per tier instead of
   flat members. Visible surface: 7 primitives + 6 dispatchers = **13
   schemas** even with `ARIEL_EXPOSE=all`. `dispatcher(action='list')`
   returns the catalog (first description line + Plan-B behavior hints +
   slot per member); `dispatcher(action='<tool>', args={...})` calls the
   real tool with `user_id` scoping applied.

## Slots (semantic map — `mcp_server/slots.py`)

| slot | n | tools |
|---|---|---|
| core | 7 | think, dream, forget, evolve, project, memory_hook, wake_up |
| recall | 5 | memory_recall, memory_search, memory_recall_protocol, memory_get_smart_context, memory_recap |
| context | 4 | memory_context, memory_context_inject, memory_steering, memory_compress |
| episodes | 4 | memory_episode_save/recall/list/get |
| sessions | 3 | memory_session_start/end/list |
| graph | 4 | memory_graph_add/query/nodes/edges |
| wiki | 9 | wiki_add/search/list/read/delete/summarize/link/reflect/query |
| insight | 10 | memory_query, memory_fact_blame, memory_history, memory_quality, memory_reflect, memory_stats, memory_diagnose, memory_standing, memory_procedure, memory_scratchpad |
| write | 9 | memory_remember, memory_save_typed, memory_load_rules, memory_counterfactual, memory_branch, memory_stash, memory_heal, memory_disclose, memory_skill_promote |
| review | 4 | memory_watch, memory_proposals, memory_report_card, daily_brief |
| admin | 7 | memory_api_key, memory_backup, memory_cleanup, memory_data, memory_lucidity_purge, memory_saga, memory_sync_replica |
| brief | 0 | *(legacy name — tier dissolved into review, 2026-09-07)* |

Slots are the catalog structure of the meta layer and the documentation unit;
tier membership (below) is the exposure mechanism. They are related but
distinct: e.g. `daily_brief` has slot `review`, and `memory_history` /
`memory_standing` are dual-tier (insight ∪ write) while living in exactly one
slot.

## Tiers (`ARIEL_EXPOSE` — `mcp_server/server.py`)

| tier | members | notes |
|---|---|---|
| (primitives) | 7 | always on |
| context | 7 | D1.1 protocol, recap, smart context, inject, steering, compress |
| insight | 17 | read-side analytics; dual-tier: memory_history, memory_standing |
| write | 17 | memory shaping; dual-tier members overlap insight |
| wiki | 9 | all `wiki_*` |
| review | 4 | proposals, watch, report card, daily_brief (ex-`brief` tier) |
| admin | 7 | keys, backups, data lifecycle, replication — never in the agent preset |

## Presets

| preset | expands to | surface (measured) |
|---|---|---|
| `agent` | `primitives,context,insight,write,wiki,review` | 59 |
| `operator` | `agent,admin` | 66 |
| `full` | `all` | 66 |

`ARIEL_EXPOSE=agent` is the recommended live-agent value. With
`ARIEL_META=1` on top, the same preset collapses to 13 visible schemas.

Legacy notes (both safe — unknown tier names are ignored):

- `brief` tier dissolved into `review` (2026-09-07); old strings containing
  `brief` keep working, `daily_brief` now arrives via `review`.
- The pre-C live-agent string
  `primitives,context,insight,write,wiki,brief,review` now resolves to 59
  (7 primitives incl. wake_up + the tier union).

## Grammar

```
ARIEL_EXPOSE = "primitives"          # default
             | "all"
             | preset                # agent | operator | full
             | tier[,tier...]        # context,insight,write,wiki,review,admin
ARIEL_META   = "1"                   # collapse tier members into dispatchers
```
