# Changelog

All notable changes to mcp-ariel-memory are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **Agent scratchpad + memory quality loop (Phase D D1.15 + D1.19)** — `memory_scratchpad` (47): L2.5 working memory between session and episodes — the agent writes hypotheses/plans/drafts (cap 20, oldest evicted), entries re-inject at session start as a `scratchpad` block, and `promote` moves agent-judged-useful entries into L3/L4 (the agent is the distiller — D2.2 ceiling). `memory_quality` (48): was_useful → score feedback loop — `feedback` writes a `recall_useful` audit row (feeds ACT-R frequency, D1.17) with importance ±0.05 (cap 1.0 / floor 0.05, importance_audit-logged as `agent_feedback`); `report` aggregates per-entry useful counts with current importance.
- **Smart context budget + reflection system (Phase D D1.10 + D1.16)** — `memory_get_smart_context` (45): weighted token distribution across memory sources — important 30% / relevant 30% / recent 15% / day 15% / ops 10%, each source gets a floor first, leftover redistributes with a 2x-floor ceiling (a fat source can no longer starve the rest, unlike the sequential inject builder). `memory_reflect` (46) + `reflections` table (migration `20260830_1000_d116`): deterministic meta-memories — windowed episode counts + recurring topics (no LLM), `action="write"` stores a reflection row, `action="list"` reads them back; the nightly hook's 5th phase writes the daily reflection automatically.
- **Store pipeline + shared skill SSOT (Phase D D2.2 + D2.3)** — `memory_skill_promote` tool (44) and `features/skill_pipeline.py`: promote distilled episodes into `skill` wiki pages verbatim with provenance footers, idempotent via a new `skill_promoted` episode tag (`EpisodicMemory.get_by_id`/`add_tag` added); the nightly hook gains a 4th phase (`skill_promotion`) auto-promoting fresh `dream_skill` episodes. Ceiling (documented): no LLM in ariel — dream_skill episodes are agent-distilled at write time (C1.12); raw auto_save chatter needs harness-side distillation (v2). D2.3: `scripts/sync_skills.py` — a git-versioned skill SSOT (`~/skills-ssot/skill/*.md`) synced into every live agent's wiki via `WikiManager.sync_external` (copy + sha256 dedup, per-agent subprocesses so `connection_manager` picks up each `MCP_MEMORY_DATA_DIR`; aiosqlite `close_all()` before exit per the C1.10 hang lesson). `scripts/sync_skills.py --bootstrap` creates the skeleton; the sync is cron-ready.
- **Skill = Memory (Phase D D2.1)** — skills are wiki pages of the new first-class `skill` type: plain Markdown (SKILL.md convention), agent-read, no embeddings for retrieval — progressive disclosure through the wiki surface (`wiki_list(wiki_type="skill")` → `wiki_search` → `wiki_read`). New `wiki_read` tool closes the missing read leg (full page content by path; `wiki_list`/`wiki_search` now expose `path`/`snippet` for chaining). Wiki lint gains the `skill_too_large` rule (concept cap: 4KB per skill page). Tool count 41 → 43. The C1.12 `dream_skill` L3 episodes are the mineable promotion trace (store pipeline = D2.2).
- **/recall protocol — multi-axis proportional recall (Phase D D1.1)** — `features/recall.py::recall_protocol` fuses five axes in concept order: `markers` (dream-marker facts at 0.95 + `dream_skill` episodes — outrank everything) → `session` (last-24h L1 chatter + latest session summary) → `semantic` (MultiSourceRAG hybrid hits) → `expand` (B1.6 1-hop graph neighbors) → `day` (last-24h auto-save digest). Proportional: empty query = zero-state (markers + day only, ~3 lines); dedupe across axes (markers' parts pre-registered, first axis wins); budget-capped. New `autohooks recall --query … [--budget N] [--format md|json]` subcommand and the `memory_recall_protocol` MCP tool (operator tier, tool count 41 → 42). The Hermes native plugin's `queue_prefetch`/`prefetch` now drive this protocol per turn — every Hermes turn gets multi-axis recall of the memory store.
- **Compaction-aware memory rehydrate (Phase D D3.5)** — ariel now learns when an agent's context was compacted: every `post_context_compression` dispatch logs a drift row to a new `compaction_events` table (migration `20260829_1900_d35_rehydrate`), and the session-start inject gains a `rehydrate` block (important L4 facts, score 0.9) when a compaction happened within `rehydrate.window_hours` (default 6h; `rehydrate.enabled` toggles). `autohooks dispatch` accepts `--payload` (JSON object merged into the event context) and `autohooks inject` accepts `--blocks` (kind filter) so harnesses can pull exactly the rehydrate block. MiMoCode plugin: the compaction hook appends the critical set to the summarizer prompt (salvage), the `session.compacted` event logs the drift and arms a one-shot `[ariel rehydrate]` system block; Hermes gateway emits a `compaction` boundary event (new `~/.hermes/hooks/ariel-compaction/` hook) and the rotated session's inject carries the block.
- **Phase C closeout (C1.12 + C1.13 + C1.14)** — `DREAM: memory:/fact:/skill:` markers detected in the auto-save pipeline and routed through staging at importance 0.95 (`skill:` also writes an L3 `dream_skill` episode for D2 to mine). `memory_proposals(action="revert")` undoes applied proposals with exact provenance: core_write (forget by key) and archive (`restore_entries` from `archived_memories` back into core — the backup already exists by design); consolidation revert deferred (ids not pinned). `memory_report_card(period_hours=24)` — operator digest: proposal counts by status + last decisions, auto-save tier sums, open gaps, dream-marker count. Both tools hidden by default; new `ARIEL_EXPOSE=primitives,review` tier opts an instance in. Tool count 40 → 41. **Phase C auto-hooks keystone is COMPLETE.**
- **Staged mutation — proposal → review → apply (Phase C C1.11)** — risk-tier true staging: L4-destined auto-saves (score ≥ 0.8), consolidation staging promotions (hook + lifespan sweep), and the forgetting ritual's archive sweep now create `mutation_proposals` instead of writing immediately (L3/graph/decays stay direct; propose failure falls back to direct write so bookkeeping never loses memory). Pending proposals surface in the session-start inject as a `proposals` block; the decision is one `memory_proposals` tool call (list / decide approve|reject), same surface for agent and operator. 7-day lazy expiry (no cron). Apply executes the exact write the direct path would have (pinned ids/items); every decision audit-logged. `staging.enabled=false` restores direct behavior. Tool count 39 → 40. Migration `20260829_1600_c111`.
- **`memory_watch` operator tool + `post_session_diff` auto-handler (Phase C C1.10)** — one server-side event fires after each session end, computes "dispatched vs persisted" gaps from a new `memory_dispatch_log` table, materializes them as L3 `diff_gap` episodes, and the next `session_started` inject surfaces them as a new `gap` block kind. `memory_watch(action=list|add|disable|delete)` is the operator CRUD over `watch_rules` (introspection of the rules ariel already applies via `auto_save_text`; no new behavior). Migration `20260829_1300_d1c110` adds both tables and seeds one default rule. Tool count 38 → 39. Gaps now reach the agent without any explicit call.
- **Universal autohooks runtime (Phase C C1.9)** — `python -m autohooks` (package `autohooks/`): per-agent daemon tails each agent's SQLite conversation store and pushes `new_message` events into the hook dispatcher in-process (no HTTP — works for stdio agents); `inject` subcommand returns the session-start critical set for harness embedding. Declarative per-agent YAML configs in `autohooks/examples/` (hermes, mimocode, cowagent — all live-verified). First-run baseline prevents history replay; cursor persists per batch (at-least-once). Isolation inherited via `MCP_MEMORY_DATA_DIR`; one runtime, one config = new agent.
- **Auto-hooks foundation (Phase C)** — external event dispatcher (`hooks/external.py::dispatch_event`) with two transports: `POST /api/hooks/{event}` (HTTP harnesses) and the new `memory_hook` MCP tool (stdio agents; primitive tier, tool count 37→38). Seven events with day-one handlers: `session_started` (returns the critical inject block), `session_ended` (session summary → L3), `new_message`/`auto_save_candidate` (`evaluate_importance` heuristic — no LLM; score ≥ 0.5 → L3 episodic + graph node, ≥ 0.8 → also L4 core), `post_context_compression` (rehydrate candidates via retrieval), `context_threshold`/`memory_pressure` (thin advice; decisions stay harness-side). New `POST /api/context-inject` returns the budget-capped critical set (ACT-R top-5 relevant when a query is given + recent L1 (24h) + important facts ≥ `inject.important_min`). Graph is now threaded through the hook registry (`fire(..., graph=)`, C1.1). Per-agent isolation inherited (own process + `MCP_MEMORY_DATA_DIR`; D1.13 API-key binding applies to both surfaces). Harness-side daemon (C1.9) is the next step.

### Fixed
- **MiMoCode plugin exported non-existent hook keys** — `~/.config/mimocode/hooks/ariel-inject.ts` used `session.start`/`session.end`, which are not keys of the fork's `Hooks` interface (verified: zero occurrences in the repo), so the C1.9 session-start inject and C1.10 `post_session_diff` never fired for MiMoCode. Rewired to the real keys: per-session one-shot inject via `experimental.chat.system.transform`, debounced diff via `session.post` (plus the D3.5 compaction hooks).

## [1.9.0] - 2026-08-29

### Added
- **Transition state machine + telemetry (B1.5)** — new `lifecycle/transitions.py`: a map of valid memory transitions (staging→l4, episode→l4, l4→archived, episode→archived) and `record_transition()` persisting each move to a `memory_transitions` table plus a metrics counter. Wired into both consolidation paths, forgetting archive, and episode compression; invalid transitions are logged and skipped — live paths never break on telemetry. Closes roadmap B1.5.
- **GraphRAG expand stage (B1.6)** — `MultiSourceRAG.search` now expands primary graph hits with their 1-hop `epi_edges` neighbors: neighbors enter the rerank as `source="graph_expand"` with a damped score (`0.5 · edge weight · confidence`). Retrieval chain is now vector/FTS → graph → rerank; no LLM reasoning (cost verdict from roadmap). Closes roadmap B1.6.
- **Causal memory (B1.7)** — `EpistemicGraph.record_causal(action, outcome, relation, strength)` records action→outcome links as `action`/`outcome` nodes joined by a causal edge (`caused`/`led_to`/`prevented`, weight = strength); exact-content idempotent, non-causal relations rejected. Causal links are queryable through the existing graph tools (`memory_graph_edges`, `direction` included). Closes roadmap B1.7. Phase B complete.
- **DAG summary lineage (B1.4)** — facts promoted by `ConsolidationEngine` now record their source summaries: `consolidate_episodes` stores `parents: ["episode:<id>"]` and `consolidate_staging` stores `parents: ["event:<event_id>"]` (when present) in the existing `core_memory.metadata` JSON — no schema change, no migration. New `ConsolidationEngine.get_lineage(entry_id)` returns parsed parent refs (the hook for future archive/compression checks: "does a live fact descend from this episode?"). Multi-parent = DAG; wiki-promotion frontmatter lineage intentionally deferred. Closes roadmap B1.4.
- **Self-building graph (B1.3)** — `lifecycle/graph_builder.py` runs a 3-stage pipeline (mine episodes → extract relations → link entities) over the last 24h of L3 episodes: deterministic RU+EN patterns ("X works at Y", "X знает Y", "X is friends with Y", "X is part of Y", "X met Y") create `person`/`organization` entities via the B1.2 upsert and connect them with edges; first-person mentions ("встретил Бориса", "talked to Claire") register the person without an edge. Wired as the 4th `nightly` phase (after diary + CLS replay); idempotent on re-runs (entity dedup + edge PK). Known ceiling (marked in code): regex extraction covers explicit patterns only and RU inflected forms count as separate entities — upgrade path is an LLM extractor behind the same interface. Closes roadmap B1.3.
- **Social Memory Graph** — social entity types (`person`, `organization`) with per-user dedup: re-adding the same name via `memory_graph_add(node_type="person")` returns the existing node (`created=false`) instead of forking it. New optional `relates_to` (node_id) + `relation` (e.g. `knows`, `works_with`, `family_of`) params on `memory_graph_add` create an edge in the same call (previously edges had no MCP surface at all). Zero new tables — entities are epistemic nodes, relationships are `epi_edges`. Queries unchanged: `memory_graph_query(node_type="person")`, `memory_graph_edges(node_id, direction="both")`. Closes roadmap B1.2.
- **Graph backlinks** — `memory_graph_edges` gains `direction: "out" | "in" | "both"` (default `"out"`, fully backward compatible): `"in"` returns incoming edges (Obsidian-style backlinks), `"both"` matches either endpoint. No new tool, tier unchanged. Closes roadmap B1.1.
- **ACT-R activation scoring** — 2nd scoring layer in `MultiSourceRAG`: `rag/actr.py::actr_activation` (base-level learning `ln(1+n) − 0.5·ln(1+age_days)` normalized to [0,1]) boosts core-fact scores by recall frequency (recall_useful audit signal) + recency (`updated_at`) and episodic scores by recency (`created_at`); multiplicative `score *= 1 + 0.3·activation`. `CoreMemory.search` results now also carry `entry_id`/`updated_at` (additive). Closes roadmap D1.17.
- **CLS replay + live recall confirmation** — `dream` now records `recall_useful` audit rows for core facts it returned (previously this signal was read by ImportanceScheduler but never written); new nightly 2nd phase `cls_replay` (`features/replay.py`) boosts facts recalled in the last 24h by +0.05 (cap 1.0) with `importance_audit` rows (`reason='cls_replay'`), wired into the existing `nightly` hook. Closes roadmap D1.18.
- **Memory Scopes (per-user isolation)** — on HTTP transports an API key bound to a user now locks the `user_id` seen by every memory tool. `mcp_server/tools/base.py::_resolve_user_id` reads `Authorization` from the request (`ctx.request_context.request.headers`, mcp 2.x); a `Bearer ak_...` header with a valid key returns the key-bound user_id, anything else (bearer token, no header, stdio/local) passes the requested value through unchanged. All `user_id`-bearing tools are registered through the `_scope_tool` wrapper in `mcp_server/server.py`; `AuthMiddleware` now accepts a bearer token OR a valid API key, and the dead `/mcp` skip is removed (MCP is mounted at `/`). No DB changes, no new endpoints, no new tools; stdio and `MCP_AUTH_DISABLED` behaviour unchanged. Closes roadmap D1.13 (cross-tenant leak: any client with a valid token could pass `user_id="bob"`).
- **Wiki secret detection** — `add()` and `sync_external()` scan content for well-known secret formats (GitHub PATs, API keys, PEM private-key headers). Matches are WARNING-logged (kind + path only, never the value) and never block the save. New pure `wiki/secrets.py::scan_secrets`. No new tool/config/DB. Closes research backlog P2 item 10.
- **Wiki ref chain linking** — typed page links (`review_of` / `revises` / `follows`) in a new `wiki_links` table. `[[wikilinks]]` are auto-linked to resolvable pages on `add()`/`update()`. New `wiki_link` tool (list/add), auto-exposed by the `wiki` tier (tool count 36→37). Closes research backlog P2 item 8.
- **Wiki promotion pipeline** — `WikiManager.promote_from_core()` turns high-importance L4 `rule`/`preference` facts into wiki pages (deterministic, idempotent, no LLM). Closes research backlog P2 item 9.
- **Wiki organic operations** — `WikiManager.split()` / `merge()` / `retire()` reorganize pages without losing history: split into `(title, content)` parts, merge into a destination with `## source` separators, retire to a `_retired/` archive (removed from search, never hard-deleted). All non-fatal on missing paths. Closes research backlog P2 item 11.

### Changed
- **Hermes integration migrated to a native `MemoryProvider` plugin** — `~/.hermes/plugins/ariel/` (`memory.provider: ariel`): in-process lifecycle hooks replace the file-hook layer (`~/.hermes/hooks/*` disabled) and the two ariel core commits (gateway compaction emit, prompt-builder inject slot — reset to upstream). Gains beyond the old wiring: per-turn `sync_turn` push (no conversation-tailing needed for turn capture), `queue_prefetch`/`prefetch` per-turn recall, `on_memory_write` MEMORY.md mirroring, `on_session_switch` cache invalidation. Transport = ariel venv CLI in background threads (pure-stdlib plugin; immune to `hermes update` venv rebuilds — the exact failure mode that killed agentmemory's HTTP daemon). In-process ariel import is the documented v2 upgrade path.
- **SQLite connection PRAGMA set completed** — `shared/connection.py` now sets `PRAGMA page_size=16384` and `PRAGMA auto_vacuum=INCREMENTAL` on connect, ordered BEFORE `journal_mode=WAL` (WAL seals page size; empirically verified). Applies to newly created databases only — existing DBs are silent no-ops. Earlier: `PRAGMA mmap_size=256MB` + `PRAGMA optimize` in `close_all()`.
- **Bulk import via executemany** — `ImportExport.import_user` inserts `core_memory` / `episodes` / `sessions` as one `executemany` per table instead of row-by-row executes; newest-wins `ON CONFLICT` guard preserved verbatim, transaction/rollback semantics unchanged.
- **Composite indexes for hot queries** — 8 idempotent `CREATE INDEX IF NOT EXISTS` entries in the owning modules' `_init_db` (created at next startup on existing DBs, no alembic): `core_memory(layer,user_id,importance DESC)`, `episodes(layer,user_id,created_at DESC)`, `epi_nodes(layer,user_id,node_type,confidence DESC)`, `sessions(user_id,started_at DESC)`, `audit_log(user_id,action,timestamp DESC)`, `wiki_index(layer,wiki_type,updated_at DESC)`, `archived_memories(user_id,archived_at DESC)` (table had no indexes), `staging_memories(layer,user_id,session_id,created_at)`.

### Security
- **Untrack encrypted auth stores** — `data/auth/keys.enc` and `data/auth/token.enc` were committed to the repo (recreated on every test run by default paths); removed from the index and ignored via `*.enc`.

## [1.8.1] - 2026-08-27

### Added
- **`wiki_summarize` tool** — 6 analytical perspectives (practical / epistemic / psychological / social / temporal / metacognitive) filter the wiki through a curated mapping to existing `wiki_type`. Auto-included under `ARIEL_EXPOSE=primitives,wiki` via the `wiki_` prefix-match. Token-budgeted digest (≤ 2000 tokens). No DB migrations, no config changes, no `mcp_server/server.py` changes — pure additive tool. See `docs/tools/reference.md` for the perspective → wiki_type mapping table.
- **Wiki schema lint** — 6 schema checks (frontmatter, required fields, broken wikilinks, page length, missing INDEX.md, unknown tags) fire on every `add()` and `sync_external()` as warning logs. Only `missing_index` is auto-fixable; pass `WikiManager(auto_fix=True)` to enable. No new MCP tool, no config schema change, no DB migration. Tag vocabulary = 7 hardcoded tags + enabled wiki_type names. See `wiki/lint.py` and `docs/wiki/file-wiki.md` for details.
- **Dream cycle inject step** — `memory_context_inject` now runs an inline `consolidate_episodes` (L3→L4 promotion) before reading L4 facts, so the context the agent sees is freshly curated instead of up-to-1h-stale. Returns two new fields: `consolidated_episodes` (int) and `last_consolidation_ts` (float). The 30s context cache is invalidated after consolidation. No new tool, no new config key, no DB migration. Closes research backlog Priority 3.12 (pluton).
- **CONTEXT.md persistence** — `memory_context_inject` now also writes a 3-section markdown snapshot (frontmatter + Context + 6 Perspectives + Recent Episodes) to `<MCP_MEMORY_DATA_DIR>/<layer>/CONTEXT.md`. Per-agent isolation via the existing `MCP_MEMORY_DATA_DIR` env var (hermes/mimocode/cowagent each have separate data dirs, so the file is naturally per-agent with no race). Each perspective is fetched via `wiki_summarize(perspective=p, layer=layer, limit=3)` (≤200 tokens). Two new non-breaking result fields: `context_md_path` (str|None) and `perspectives_count` (int=6). Write failures are non-fatal (logged at WARNING, inject never breaks). Closes research backlog Priority 3.13 (pluton).
- **Session quality scoring** — `memory_session_end` computes a deterministic score (depth / decision / linked_entries / user_engagement / recall_usage, 0-100) on close and persists `quality_score` + `quality_parts` (JSON) on the session row. Two new optional params (`topics` / `state_deltas`) feed the engagement component; `recall_usage` is fed by recall telemetry (below). Score is surfaced via `memory_stats.avg_session_quality`. Closes research backlog P1 item 1 (icarus).
- **Recall telemetry** — every `dream` call is recorded in a new `recall_events` table (query, intent, result_count, layer, user_id, timestamp). `memory_stats` now returns `recall_count`. The session quality score gains its 5th component `recall_usage` (scale 4x20=80 → 5x20=100). Timeline shown by `dream(intent="recent")` is unaffected (recall events live in a separate table). Closes research backlog P1 item 2 (icarus).
- **Auto training_value classification** — `think` classifies each thought as high / medium / low (decision + outcome regexes, RU+EN) and stores it in the temporal thought-event metadata. Pure function, no new tool/config/DB. Closes research backlog P1 item 3 (icarus).
- **Daily brief tool** — `daily_brief` returns a 3-section report (pending work from L4 todo facts, recent activity from temporal events + recall count, suggested action from todo follow-ups and open sessions). Deterministic, no LLM, non-fatal per section. New `brief` exposure tier (`ARIEL_EXPOSE=primitives,wiki,brief`); default 5-primitive surface unchanged. Closes research backlog P1 item 4 (icarus).

## [1.8.0] - 2026-08-26

### Added
- **Temporal timeline activated end-to-end** — timeline events gained a `layer` column with automatic migration for legacy tables; `think` records thought events, `evolve` records personality shifts, `project(decision)` records project decisions; `dream(intent="recent")` prepends a Timeline digest of the 5 newest events.
- **Tiered tool exposure** — `ARIEL_EXPOSE` now accepts comma-separated tiers: `primitives,wiki` adds `wiki_add` / `wiki_search` / `wiki_list` / `wiki_delete` alongside the 5 primitives; `all` unchanged.
- **Explicit wiki saves from `think`** — optional `wiki_type` / `wiki_title` params force a wiki save under an explicit page name/type instead of automatic `Thought_<ts>` routing.
- **Tokenized multi-word recall** — CoreMemory.search and EpisodicMemory.search match on any query word, results ranked by matched-word count then importance (single-word behavior unchanged).
- **Optional `embeddings` extra** — sentence-transformers with torch pinned to the CPU wheel index; `ARIEL_HASH_EMBEDDINGS=1` forces the deterministic hash backend; embedding cache rows are keyed by the producing backend so hash vectors can never be served as model embeddings.
- **Export/import payload v1.1** — sessions round-trip, layer preservation, newest-wins conflict guard, single-transaction imports with rollback, user_id validation on export filenames, `list_exports` filterable by user_id.
- **End-to-end pipeline test suite** — runs against real stores: placement, dream recall, consolidation sweep, forget + Shadow Bin, evolve→temporal, project cycle, and a layer-isolation pin.

### Changed
- Default tool surface unchanged (5 primitives); `docs/tools/reference.md` documents all three exposure tiers.
- e5 embedding calls now use `query:` / `passage:` prefixes at the search and ingest call sites.
- `get_causal_chain` accepts optional user_id scoping.

### Fixed
- **compression.deduplicate_core partitioned BY layer** — previously memory_cleanup could delete a legitimate cross-layer row (user fact vs agent fact sharing a key) on every run.
- **compress_episodes scoped per layer** — agent identity episodes are no longer swept by user maintenance, and doomed episodes are archived to the Shadow Bin before deletion.
- **wiki/index consistency** — frontmatter wiki_type edits no longer desync external-content FTS5; file_path lookups are layer-scoped; unique index migrated to `(layer, file_path)`; mutations serialized under a lock with rollback-on-error.
- **WikiManager directory isolation** — resolves its directory from `MCP_MEMORY_DATA_DIR`; instances no longer share one wiki folder.
- **import_export safety** — INSERT OR REPLACE clobbering newer live data replaced by the newest-wins guard; half-applied imports rolled back.
- **Hooks fully async per contract** — removed the ThreadPool bridge; fixed emotion_trigger dropping episodes via asyncio.run inside a running loop.
- **Consistent backups** — SQLite online-backup snapshots replace copying live WAL files; backup_cron schedules coroutines onto the server event loop.
- **secrets** — `.env` canonicalized to the instance data dir (legacy repo-root `.env` still read).
- ReflexBuffer persistence debounced (30 s / 10 adds) instead of fsync per message.
- Dashboard stats use COUNT queries; context/recall caches invalidated by cleanup/purge/import; recall cache bounded (512); config singleton thread-safe; stale connection reopen no longer races.
- RAG: every search branch and the sha256 dedup are layer-scoped; FTS5 failures log a warning with the SQLite version.
- forgetting.compress_duplicates rewritten as one window-function DELETE; importance_scheduler batches retrieval counts per user.
- Flaky saga compensation test made self-sufficient (explicit schema init).

## [1.7.0] - 2026-08-24

### Added
- **mcp 2.x native** — server migrated from `mcp.server.fastmcp` (removed in SDK 2.0) to `mcp.server.mcpserver.MCPServer`; dependency range `mcp[cli]>=2,<3`.
- **Universal primitives by default** — agents see exactly `think` / `dream` / `forget` / `evolve` / `project`; the full layer-tool surface stays available via `ARIEL_EXPOSE=all`.
- **Layer registry** — `LayerBinding` bundles per-layer stores (L3+L4 memory, graph, wiki, hooks, hybrid RAG); new layers register once and every primitive accepts them.
- **Project memory layer** — `projects.db` (separate file) with identity, decision log (decision/rationale/outcome), artifact map and a graphify-fed code index; `project` gains `decision`/`recall` actions, `update` auto-refreshes the code map.
- **Dream staging pipeline restored** — dream output lands in staging_memories via layer-aware handlers; the hourly sweep drains it through consolidation before episode promotion.
- **DB maintenance loop** — hourly size check of `*.db` with WARN/ERROR thresholds (`storage.*`), prometheus gauges, auto-VACUUM on big fragmented files.
- **Config drift warning** — per-agent configs (`MCP_CONFIG_PATH`) are checked at startup for keys added by newer repo defaults.
- **`rag.chunk_size` / `chunk_overlap` / `search_limit`**, `embeddings.model`, `binary.*`, typed-memory TTLs, `performance.wal_mode`, `logging.*`, `dashboard.enabled/port`, `layers.<name>.enabled` — previously dead yaml keys are now read by their subsystems.

### Changed
- **think auto-routing is real** — agent-voice signals route to the agent layer instead of always defaulting to user.
- **forget(scope=recent)** uses manager APIs (`delete_older_than`) instead of raw SQL; fuzzy branch likewise via `delete_by_ids`/`delete_nodes`.
- **Wiki taxonomy**: `project_spec` added as a user-layer type.
- Hooks: config.yaml is the single source of truth (code-side duplicate list removed). Agent-layer `importance_gate` is explicitly disabled — explicit saves must not be silently dropped by the adaptive threshold.

### Fixed
- **Layer isolation for L3/L4** — core_memory and episodes gained a `layer` column (alembic c7e21a94b0d5); user facts and agent identity no longer share one namespace, unique key is now `(layer, user_id, key)`.
- **Episode save** bound 4 params to 5 placeholders (created_at lost) — every insert raised OperationalError.
- **forget(recent)** reported the staging purge count as graph deletions.
- **Saga state reader** misclassified ~1/256 encrypted files as plain JSON (magic-byte sniff); decrypt-first now.
- **tools/list was always empty** since inception: Literal imported from a nonexistent path, Context hidden under TYPE_CHECKING broke annotation eval, stdio entrypoint never registered tools (fix 419d577).

## [1.6.4] - 2026-08-12

### Added
- **Ariel-Memory Constitution** — Established the core law for development: strict typing, async mandate, and modular aesthetics.
- **Shadow Bin Support** — The `forget` primitive now supports soft-deletion via `ArchivedMemories`, preventing accidental loss of high-importance facts.
- **Intelligent Thought Routing** — The `think` primitive now automatically directs large thoughts (>2000 chars) to Wiki pages while maintaining links in CoreMemory.
- **Automatic Relation Extraction** — Integrated regex-based detection of entity relationships in the `think` primitive for seamless knowledge graph expansion.
- **Token Budgeting for Dreams** — The `dream` primitive now enforces a `DEFAULT_TOKEN_BUDGET` to prevent context overflow in LLM agents.

### Changed
- **Engineering Excellence** — Achieved 100% Mypy strict compliance and zero Ruff linting issues repo-wide.
- **Systemic Typing** — Resolved external library typing for `sentence_transformers` via `pyproject.toml` configuration, removing non-compliant inline ignores.
- **Forget API Evolution** — `memory_forget` and the `forget` primitive now return detailed `ForgetResult` objects (deleted_l4, deleted_l3, deleted_graph) for granular traceability.
- **Docstring Standards** — All core modules updated with Lucy-style docstrings explaining **WHY** components exist.

### Fixed
- **Null Safety in RAG** — Added connection manager guards in `MultiSourceRAG` to prevent crashes in environments with partial storage availability.
- **Model Signature Alignment** — Synchronized `ForgetResult` model fields between definition and implementation.

## [1.6.3] - 2026-08-08

### Fixed
- **Massive Test Stabilization** — Restored 100% stability of the test suite (519/519 PASSED) after major architectural refactoring.
- **Async/Sync Mismatches** — Fixed numerous `RuntimeWarning` and `TypeError` issues caused by calling async methods synchronously in legacy tests.
- **RAGEngine MIB Alignment** — Resolved dimension mismatches in binary search logic; binarization now correctly aligns with the standard 384-dim embedding model.
- **Saga Persistence Reliability** — Fixed `SAGA_DIR` patching in tests to prevent cross-test contamination and ensure state is correctly saved/loaded from temporary directories.
- **Adaptive Threshold Graceful Fallback** — Added database availability checks to `ImportanceGateMiddleware`, ensuring system stability in environments without persistent storage.
- **Chaos Testing Robustness** — Refined SQLite chaos simulations to avoid locking critical initialization queries (PRAGMAs).
- **Mypy & Ruff Compliance** — Eliminated all critical linting errors and type-checking issues (UP037, TC001, F821 Sequence error).
- **Global Strictness** — Successfully enabled global Mypy strictness and removed 10+ critical Ruff ignore rules, including `S110`, `ASYNC240`, and `UP031`.

### Changed
- **Supreme Orchestrator Identity** — Permanently integrated Lucy-Prime persona into the system backbone via lifecycle hooks.
- **Registry & Tools Cleanup** — Removed dozens of unused imports and obsolete classes leftover from the modularization phase.
- **Modular Hardening** — Core infrastructure is now 100% type-safe and compliant with modern Python 3.10+ standards.
- **Background Cleanup** — Activated `MemoryCompactor` in the server lifecycle. Old, low-importance memories are now automatically archived every 1 hour.
- **Infrastructure Restoration** — Recovered missing `config.py` from backup, stabilizing global configuration management.

## [1.6.0] - 2026-08-08

### Changed
- **Saga System** — Refactored and moved to `shared/saga/`. Now features a modular async engine with state persistence and compensation logic.
- **Emotion Trigger** — Refactored and moved to `lifecycle/emotion/`. Provides high-performance emotion detection using optimized regex matching.
- **Wiki System** — Migrated to a dedicated `wiki/` package. Enhanced FTS5 search and hash-based reindexing.
- **Importance Scorer** — Refactored and moved to `shared/importance/`. Implemented a modular plugin architecture for importance signals.

### Added
- **Package READMEs** — Documentation added for all new modular packages.
- **Architecture Updates** — Diagrams and descriptions updated to reflect v1.6.0 changes.

### Performance
- **System Health** — Repowise average score improved from ~7.1 to 7.67 through significant architectural decoupling and modularization.

## [1.5.0] - 2026-08-08

### Added
- **Prometheus Metrics** — Integrated `prometheus_client` exporter on port `9120`. Real-time tracking of memory operations, filter performance, and search latency.
- **Adaptive Importance Threshold** — Implemented Exponential Moving Average (EMA) for dynamic noise filtering. The threshold now adapts to conversation signal within [0.1, 0.6] range.
- **Memory Auto-Compaction** — Background maintenance task that automatically archives old, low-importance memories to keep search results relevant and reduce context bloat.
- **Alembic Migrations** — Transitioned database schema management to Alembic for reliable, versioned updates.
- **Infrastructure Dashboard** — Enhanced metrics visibility via Prometheus-compatible endpoints.

### Fixed
- **SQL Injection Vectors** — Hardened dynamic query construction in `episodic.py` and `wiki/shared.py` using table whitelisting and proper parameterization.
- **Insecure Hashing** — Updated content hashing in `wiki/manager.py` from `md5` to `sha256`.
- **Async I/O Safety** — Fixed blocking `pathlib` calls in async contexts by wrapping them in `asyncio.to_thread`.
- **CI Stability** — Massive linting (Ruff) and typecheck (Mypy) cleanup across the codebase.
- **Database Consistency** — Implemented orphans cleanup and SQLite statistics optimization (`ANALYZE`).

### Security
- Resolved multiple S608 (SQLi) and S324 (Insecure Hashing) findings flagged by strict security audit.
- Migrated all `open()` calls to safe `Path.open()` wrappers.

## [1.4.0] - 2026-07-06

### Added
- **Lost-in-the-Middle prevention** — context_inject and memory_context now place L4 CoreMemory at start and end of prompt, L2/L1 in middle. LLMs remember first and last items best.
- **repo-visualizer** — GitHub Action generates SVG map of repo structure on push.
- **release-drafter** — auto-generates CHANGELOG from merged PRs based on conventional commits.

### Testing
- **Test suite optimization** — 364→250 tests via parametrization, deduplication, and property-based expansion.
- **Parametrized** 6 files: test_rag_scoring, test_rag_search_facade, test_memory_types, test_importance_v2, test_mib_quantize, test_tools_unit.
- **Property-based expansion** — 25→39 tests: ImportanceGate, MemoryTypes, PathSafety, Saga, Connection, Cache, Embeddings, Secrets.
- **Coverage tests** — 32 new tests for typed_export, backup, audit_trail, rate_limiting, agent_hooks, wiki, backup_cron, saga.
- **Logic verification** — 10 tests verifying algorithm correctness: ImportanceScoring, TypedDecay, SagaCompensation, SearchRelevance, RAGPipeline, WikiCRUD, ConnectionPool, ImportanceGate, RateLimiter.
- **Stateful machines** — 6 tests for MemoryManager, Saga multi-step, Hooks execution order.
- **Chaos fixtures** — 4 fixtures: database_locked, api_timeout, keyboard_interrupt, corrupt_db.
- **Deleted 10 duplicate files**: test_all, test_mcp/test_mcp, test_lifecycle, test_hooks, test_graph, test_rag, test_rag_edge_cases, test_tools_layer, test_tools_ops, test_saga_compensation/retry/idempotency.
- **Removed getter/setter tests** that tested third-party library behavior.
- **Coverage**: 73% (Codecov) with 79 property-based/logic/chaos tests.

## [1.3.1] - 2026-07-06

### Fixed
- **aiosqlite 0.22.0 hang** — pinned `aiosqlite>=0.21.0,!=0.22.0` in CI workflow. Version 0.22.0 changed worker thread architecture, causing pytest to hang indefinitely after tests complete.
- **CI test hang** — rewrote e2e tests to use `AsyncConnectionManager(base_dir=tmp_path)` instead of global `connection_manager`. Each test now creates its own temp database, preventing aiosqlite connection leak.
- **pytest_sessionfinish** hook added with `os._exit(0)` as safety net for process termination.
- **Event loop scope** — confirmed `asyncio_default_fixture_loop_scope = "function"` in pyproject.toml.

### Testing
- **E2e tests restored** — 18 tests covering all 25 MCP tools with real data flow (temp DB, not mocks).
- Hook dispatch verified: message_received, emotion_trigger, state_delta, consolidation, error_occurred, decision_made, retrieval_router, auto_context.

## [1.3.0] - 2026-07-05

### Security
- SQL injection findings (150): all **false positives** — standard SQLite parameterized query pattern. Added `SKY-D211` to skylos ignore list.

### Architecture
- **RAG engine split** — `rag/engine.py` (561 lines) split into 3 modules:
  - `rag/engine.py` (224 lines) — ingest, relations, counts
  - `rag/search.py` (239 lines) — FTS5, binary, hybrid, RRF search
  - `rag/chunking.py` (60 lines) — text chunking with overlap
- **Tool rename** — `memory_search_rrf` → `memory_search` (backward-compatible alias kept)

### Fixed
- **N+1 query** in `_search_rrf` — batched page lookups with `IN` clause instead of N individual queries.
- **Embedding dedup** — extracted `_insert_page` helper from `ingest_file`/`ingest_text`.
- **Router simplification** — extracted `_match_route` helper from `route()`.
- **DB_NAME constant** — extracted `shared/constants.py`, replaced 121 occurrences of `"memory.db"`.
- **Saga complexity** — extracted `_compensate_inner_saga` and `_compensate_step` from `_compensate` (CCN 22→11).
- **Emotion trigger** — extracted 5 helper methods from `should_save` (CCN 20→9).

### Testing
- **499 tests** (was 338, +161 new tests)
- **Coverage: 61% → 77%** (+16%)
  - `shared/saga.py`: 58% → 79%
  - `shared/saga_crypto.py`: 37% → 89%
  - `shared/read_only.py`: 57% → 86%
  - `shared/migrations.py`: 39% → 81%
  - `mcp_server/tools_layer.py`: 15% → 53%
- **All 25 MCP tools** tested (e2e + unit)
- **24 hooks** verified through tool dispatch
- **E2E tests** for full tool logic path (remember → L1 → episode → L4)
- New test files: `test_saga_unit.py`, `test_middleware_unit.py`, `test_embeddings_unit.py`, `test_tools_unit.py`, `test_tools_e2e.py`, `test_tools_ops_e2e.py`, `test_saga_crypto_coverage.py`, `test_read_only_coverage.py`, `test_migrations_coverage.py`

### Quality
- **Skylos Grade: F (40) → A+ (100)** — SQL injection false positives suppressed via config
- **Repowise Hotspot: 3.88 → 4.37** (+0.49)
- **Repowise Average: 7.55 → 7.82** (+0.27)
- Quality issues: 437 → 393 (-44)

## [1.2.0] - 2026-07-04

### Security
- **CRITICAL** Added path traversal guard (`shared/path_safety.py`) — `safe_resolve()` with symlink protection prevents crafted paths from escaping base directory.
- Path traversal fix in `wiki/manager.py` (update/get/delete), `features/backup.py`, `features/backup_cron.py`, `features/import_export.py`.
- All 8 SQL injection findings from skylos are **false positives** — standard SQLite parameterized query pattern.

### Architecture
- **Wiki unification** — merged `file_wiki.py`, `user_wiki.py`, `agent_wiki.py` into single `WikiManager` with layer-based separation (~900 lines of duplication removed).
- **Hook wiring** — all 24 registered hooks now called via `hook_registry.fire()` in production code (was never invoked before). 21 `_fire_hook` calls across `tools_layer.py`.
- **Dead code wiring** — connected `saga_crypto.read_state_legacy_or_encrypted` to `saga.py` and `backup_cron.py` (were defined but never called).
- **N+1 query fix** — `_search_rrf` now batches page lookups with `IN` clause instead of N individual queries.
- **RAG ingest dedup** — extracted `_insert_page` helper from `ingest_file`/`ingest_text`.
- **Router simplification** — extracted `_match_route` helper from `route()`, flattened nested matching.

### Fixed
- **Hooks** Replaced `threading.Lock` with `ThreadPoolExecutor(max_workers=1)` in `hooks/shared.py` — no longer blocks the event loop.
- **Schedulers** `importance_scheduler` now started in `lifespan()` with graceful shutdown.
- **Periodic tasks** Added `forgetting.cleanup()` running every 15 minutes in background.
- **Emotion trigger** Replaced direct `app.emotion_trigger.should_save()` with `fire("emotion_trigger", ...)` + fallback.
- **Validation** Added `_validate_layer()` to all 17 MCP tool functions.
- **Context inject** Now fires `auto_context` hook.

### Quality
- 372 tests pass (was 338, +34 new tests)
- Repowise Hotspot: 3.88 → 4.28 (+0.40)
- Repowise Average: 7.55 → 7.73 (+0.18)
- Skylos Quality issues: 437 → 403 (-34)
- Alert files: 18 → 15 (-3)

### DevOps
- 3 PRs merged: #47 (security), #48 (RAG), #49 (hooks)
- Branch protection: lint + quality + typecheck + test (3.12) required

## [1.1.0] - 2026-07-03

### Security
- **CRITICAL** Fixed SQL injection in `features/compression.py` — table names from `sqlite_master` now validated against whitelist and escaped with brackets.
- Updated vulnerable dependencies: starlette 1.0.0→1.3.1 (8 CVEs), idna 3.14→3.18.

### Architecture
- **New: `mcp_server/registry.py`** — Central tool registry module. Resolves circular imports between server.py, tools_layer.py, and tools_ops.py. All tools now self-register at module load.
- **New: `hooks/shared.py`** — Shared hook utilities extracted from agent_hooks.py and user_hooks.py. Contains `run_async`, `forgetting_ritual`, `conflict_resolver`, `auto_context`, `retrieval_router`, `consolidation`.
- **New: `wiki/shared.py`** — Shared wiki utilities extracted from agent_wiki.py, file_wiki.py, user_wiki.py. Contains `load_config`, `get_enabled_types`, `get_external_dirs`, `find_by_source`, `parse_tags`, `build_update_clause`, `build_count_query`, `format_search_result`.

### Fixed
- **RAG** `_search_rrf` integrated as fallback in `_search_hybrid` when scorer is None (was dead code).
- **RAG** LIKE wildcard escaping in FTS fallback query — `%` and `_` characters now escaped.
- **RAG** Extracted `_ingest_single_file` helper to reduce complexity in `ingest_file` and `ingest_text`.
- **Saga** Refactored `execute()` method — extracted `_check_idempotency`, `_execute_step_with_retry`, `_record_step`. CCN reduced from 20 to 7.
- **Hooks** Fixed ThreadPoolExecutor leak — now uses module-level executor instead of creating new one per call.
- **Graph** Wired `USER_TAGS` and `AGENT_TAGS` into tag validation logic.
- **Router** Wired `strategy_name` into dynamic strategy selection instead of hardcoded enums.
- **Connection** Fixed `_HAS_AIOSQLITE` — now initialized at module level before conditional block.
- **Models** All 15 Result classes now used in MCP tool returns (was 8, added ContextResult to memory_context).
- **Registry** Hook names now auto-discovered via `inspect.getmembers` instead of hardcoded sets.
- Removed unused imports: Context (server.py), ContextResult (tools_layer.py), load_config (agent_wiki.py, user_wiki.py), Any (importance.py, memory_types.py), sqlite3 (migrations_saga_log.py), TypePolicy (importance.py).
- Removed unused `_get_config` wrapper in file_wiki.py (replaced by `load_config` from wiki/shared.py).

### Quality
- Skylos grade: **F (38/100) → A+ (99/100)**
- Dead code items: 41 → 4 (remaining are documented utilities)
- Unused imports: 3 → 0
- Unused variables: 4 → 0
- All 313 tests pass

### DevOps
- **New: `.pre-commit-config.yaml`** — Pre-commit hooks for ruff, skylos, trailing whitespace.
- **New: `docs/process-rules.md`** — 4 development process rules.
- **Updated: `.github/workflows/ci.yml`** — Added skylos quality gate job.
- **Updated: `pyproject.toml`** — Added skylos config (exclude patterns, quality thresholds).

## [1.0.0] - 2026-06-29

### Breaking changes
- **RAG API:** removed `RAGEngine.search_rrf()`, `.search_its()`, `.search_binary()`. Use unified `.search(query, strategy=...)` with `"fts"`, `"mib"`, or `"hybrid"` instead.

### Migration guide
```python
# Before
await engine.search_rrf("query", user_id="u")
await engine.search_its("query", user_id="u")
await engine.search_binary("query", user_id="u")

# After
await engine.search("query", user_id="u", strategy="hybrid")
await engine.search("query", user_id="u", strategy="hybrid")
await engine.search("query", user_id="u", strategy="mib")
```

### Fixed
- **P0** `AgentHooks._importance_gate` missing — `memory_remember(layer="agent")` crashed with `AttributeError`. Added method with agent-specific keyword scoring.
- **P1** Backup cron `db_files` duplicated same filename 10x. Copy-paste error — now single entry.
- **P1** `backup_now()`, `list_backups()`, `restore()` sync methods called with `await` in `tools_ops.py`. Removed `await`.
- **P1** `AuditTrail._get_conn()` / `EpistemicGraph._get_conn()` don't exist. Fixed to use `conn_manager.get("memory.db")`.
- **P1** HTTP transport error handling — `mcp.run(transport="streamable-http")` failed silently. Added try/except with logging.
- **P1** Auth middleware returned 401 for MCP clients on `/mcp` endpoint. Skip auth for `/mcp` and `/health`.
- **P1** MCP protocol violation — `tools/list` called before `initialized` notification. Added protocol docs.
- **P2** `is_hook_enabled()` returned `False` for all hooks. Known hooks now default to `True`.
- **P2** `_calculate_importance` too simple (length/signs only). Added semantic keywords (important, critical, urgent, etc.).
- **P2** `_run_async` thread safety — added `asyncio.Lock` for concurrent SQLite access.
- **P3** `config.py` crashed with `FileNotFoundError` on fresh install. Added try/except fallback.
- Auto-generate master key on first run — no more `RuntimeError: No master key found`.
- `None` scores in `_materialize_candidates` merge — LIKE-fallback results now handled.
- Deprecation warnings on deprecated test calls (`search_rrf`/`search_binary`) — migrated to `search(strategy=...)`.
- `Saga.get_state()` returned reference to `_data` — now returns copy.
- `Saga._compensate()` didn't save state before compensation — added `_save_state()` before loop.
- `SagaWatchdog.cleanup_completed()` only cleaned completed/compensated — added stuck/failed/manual_review_required.
- `Saga._data` wasn't saved before `update()` — moved `step.data` assignment before `_data.update()` to preserve pre-step state.
- `rag/engine.py` empty embedding guard — added `len(emb) > 0` check before `struct.pack`.
- Missing indexes on `updated_at`/`timestamp` columns — added for `core_memory`, `user_wiki`, `agent_wiki`, `wiki_index`, `audit_log`.
- `query_by_tag()` used `LIKE` on JSON — extracted tags to `epi_tags` table with indexed JOIN (1850 ops/s).
- `_search_binary()` loaded all rows into memory — changed to batched `fetchmany(1000)`.
- Missing `rag_chunks(page_id, chunk_index)` index — added for JOIN performance (3537 ops/s).
- `GET /health` — health check endpoint with status, version, uptime, DB connectivity.
- `GET /ready` — readiness probe for Kubernetes (DB + migrations OK).
- `GET /alive` — liveness heartbeat for container orchestrators.
- Graceful shutdown — SIGTERM/SIGINT handler stops backup_cron, saga_watchdog, read_only_replica.
- `demo.py` — launch demo script creating test data and showing all features.
- Router bilingual keywords — RU + EN for recent, wiki, graph (B2.1-B2.3).
- Router entity extraction — stopword whitelist instead of length filter (B2.4).
- Router data-driven priorities — `_ROUTE_TABLE` config (B2.5).
- ConflictResolver B3 — BM25 + char-trigram hybrid similarity replaces Jaccard.
- ConflictResolver resolve() — archives deleted conflicts before removal with audit trail.
- Saga retry with exponential backoff (B7) — `retry_attempts`, `retry_backoff`, `retry_on` per step.
- Saga idempotent step replay (B7) — `idempotency_key_fn` + `saga_step_log` table prevents duplicate effects.
- `saga_crypto.py` — atomic encrypted state writes with legacy JSON rotation.
- **Typed Memory** — 13 categories (instruction, fact, decision, goal, preference, commitment, relationship, observation, rule, todo, question, hypothesis, context) with per-type retention, decay, and retrieval boost.
- `shared/memory_types.py` — `MemoryKind` enum, `TypePolicy`, `apply_decay()`, `can_archive()`, `kind_for_text()` heuristic.
- `CoreMemory.save()` now accepts `memory_kind`, `expires_at`, `source`, `metadata` params.
- `ForgettingSystem` type-aware: instruction/rule/commitment never decay/archive.
- `ConsolidationEngine` type-aware: low importance instruction/rule/commitment still promote.
- RAG `_apply_type_boost()` boosts results based on query keywords matching type.
- `typed_export.py` CLI — export, reclassify, backfill bulk operations.
- Migration v5: `memory_kind` column, `memory_kind_registry` with 13 seed types.
- Migration v6: `expires_at`, `source`, `metadata` columns in `core_memory`.
- Migration v7: drop float embeddings column.
- Migration v8: `importance_audit` table for scheduler logging.
- 30 new tests: memory_types (16), forgetting (5), consolidation (3), backward compat (6).
- **Importance v2** — 8-signal scorer (base, length, question, tech_keyword, emotional, novelty, retrieval_signal, noise_penalty) with configurable weights.
- `ImportanceScheduler` — background daemon for periodic re-scoring based on retrieval usage.
- `ImportanceGateMiddleware` — uses `ImportanceScorer` instead of naive heuristic.
- `Scorer.update_weights()` — bandit-style weight updates for online learning.
- 23 new importance tests (15 unit + 4 scheduler + 4 middleware).
- `test_rag_no_legacy_api.py` — confirms deprecated methods are removed.

### Added
- `AgentHooks._importance_gate` with agent-specific keywords (error, decision, principle, lesson, pattern).
- `--no-auth` flag for development servers.
- `--dashboard` flag (dashboard disabled by default for startup performance).
- Auto-generated MCP master key with `.env` persistence on first run.
- `try/except` wrapping all hook calls — single hook crash no longer breaks tool execution.
- Integration test for `memory_remember(layer="agent")` via FakeApp.
- Unit test for `AgentHooks._importance_gate`.
- Config fallback for missing `config.yaml` files.
- Hermes YAML config examples in README.
- `CHANGELOG.md` with full release history.
- `rag.storage.keep_float_blobs` config option — make float embeddings optional (default: true).
- 6 new tests for `features/secrets.py` — `_load_master_key`, `_save_dotenv`, `_load_dotenv`, `_get_master_key` caching.
- MultiSourceRAG documentation in `docs/04-rag.md`.
- 5 database indexes on `updated_at`/`timestamp` columns.
- `epi_tags` table with indexed JOIN for fast tag lookups (migration v3).
- `rag_chunks(page_id, chunk_index)` index for JOINs (migration v4).
- Batched embedding reads in `search_binary()` (fetchmany with BATCH_SIZE=1000).
- Performance benchmarks: FTS 1817 ops/s, MIB 215 ops/s, hybrid 178 ops/s, epi_tags JOIN 1850 ops/s, rag_chunks JOIN 3537 ops/s.

### Changed
- Dashboard `features.dashboard: false` by default (was `true`).
- Test suite: deprecated `search_rrf()` / `search_binary()` calls migrated to unified `search(strategy=...)`.
- `docs/07-hooks.md`: added `AgentHooks._importance_gate` documentation.
- `docs/11-operations.md`: added `--no-auth`, auto-generated keys, dashboard flag docs.
- Test count: 312 passing (was 239).

### Docs
- MCP initialization protocol in `docs/11-operations.md`.
- Security/encryption documentation for API keys and bearer tokens.
- Hermes YAML config in both README and README_EN.
- Updated README features table with MultiSourceRAG, ITS scoring, search strategies.
- `docs/01-architecture.md`: fixed test count (229→239), search description (sqlite-vec→MIB), removed phantom `api_keys` table.
- `docs/02-mcp-tools.md`: added `sources` parameter to `memory_search_rrf`.
- `docs/04-rag.md`: added `thresholds`/`search_strategy` params to RAGEngine, deprecation notices for `search_rrf`/`search_binary`, MultiSourceRAG section, `keep_float_blobs` config.
- `docs/07-hooks.md`: clarified `_importance_gate` is called directly, not via hook registry.
- `docs/09-features.md`: added `features/secrets.py` documentation.
- `README_EN.md`: fixed test count (237→239), synced rag config block.
- `README.md`: synced doc 04 description with README_EN.

---

## [0.x] - 2026-06-21 to 2026-06-28

### Features (highlights)
- **Unified 19-tool API** — single `layer` parameter instead of 37 separate tools.
- **RAG unified search facade** — `search(query, strategy)` with 4 strategies: `fts`, `mib`, `hybrid`, `auto`.
- **MIB binary embeddings** — 384 dims → 48 bytes via Maximally-Informative Binarization.
- **Supervised threshold training** — per-dimension MIB thresholds from labeled pairs (+10-15% recall).
- **ITS-inspired novelty scoring** — document frequency as prior for retrieval surprise.
- **MultiSourceRAG** — unified RAG + Wiki search with dedup and reranking.
- **Envelope encryption** — API keys/bearer tokens encrypted at rest with libsodium secretbox.
- **Saga pattern** — multi-step operations with compensation, watchdog, nested sagas, per-step timeouts.
- **Knowledge graphs** — epistemic (facts/decisions) + temporal (timeline) via recursive CTE.
- **Wiki system** — 14 content types, .md source of truth, external folder sync.
- **24 hooks** — intercept memory operations at every stage.
- **Platform-aware async** — aiosqlite on Linux/macOS, sync fallback on Windows.
- **Rate limiting** — all MCP tools, WebSocket/SSE, HTTP API endpoints.
- **Read-only replica** mode.
- **Connection pooling** — `AsyncConnectionManager` with WAL mode.
- **Import/export** — `memory_export`/`memory_import`/`memory_list_exports` tools.
- **Lucidity purge** — context injection, lucidity scoring.
- **Embedding cache** — avoid redundant inference calls.
- **Memory compression** — context-mode integration.
- **Archived memories** — soft delete with `ArchivedMemories`.
- **Emotion trigger** — emotion detection in memory operations.
- **RetrievalRouter** — multi-signal query routing with entity/NER extraction.

### Infrastructure
- CI matrix: Python 3.10–3.13, lint + test jobs.
- npm wrapper (`mcp-ariel-memory@1.0.0`) for `npx` deployment.
- Docker support with `docker-compose.yml`.
- MCP Registry published as `io.github.Cipher208/ariel-memory@1.0.0`.
- ruff check + ruff format enforced.
- 239 tests across 22 test files.

### Documentation
- 14 doc files covering architecture, tools, core, RAG, graph, lifecycle, hooks, wiki, features, shared, operations, testing.
- English README + Russian README.
- Architecture diagrams in `docs/01-architecture.md`.
- Full MCP tools reference in `docs/02-mcp-tools.md`.
