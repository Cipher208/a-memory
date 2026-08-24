# Control Map & Config Audit — mcp-ariel-memory

> **UPDATE 2026-08-24 (59c798a):** the audit below triggered a config rework.
> `MCP_CONFIG_PATH` now enables per-agent configs; formerly-dead sections
> embeddings/binary/typed_memory(TTL)/forgetting.consolidate_weight_threshold/
> performance.wal_mode/logging/dashboard/layers-gate/cors are WIRED and live.
> Hooks: yaml is the single source (known_hooks duplicate removed).
> Phantom keys removed: webhooks, cross_user_learning, versioning,
> features.auth, api_keys_enabled, rag fts/vec flags, graph temporal/epistemic.
> Section table below kept as the historical audit record; live-status column
> is superseded by this banner for the listed sections.

_Audited 2026-08-24 against master @ c2c7c19. Every claim verified by grep over consumers._

## 1. Control planes (who controls what)

```
                 ┌──────────────────────────────────────────────┐
   ENV VARS      │  MCP_MEMORY_DATA_DIR   → DB root per agent   │
   ────────────► │  MCP_MASTER_KEY        → crypto master key   │
                 │  MCP_AUTH_TOKEN / _DISABLED → HTTP auth      │
                 │  ARIEL_EXPOSE          → tool surface        │
                 │  BACKUP_CRON_DISABLED  → tests               │
                 └───────────────┬──────────────────────────────┘
                                 │
   config.yaml ──► Config() ─────┤  (singleton, loaded from REPO dir —
   (repo-root)                   │   shared by ALL agent instances!)
                 ┌───────────────┴──────────────────────────────┐
   CLI FLAGS     │  --transport stdio|http  --host  --port      │
   ────────────► │  --dashboard  --no-auth                      │
                 └───────────────┬──────────────────────────────┘
                                 │
                    lifespan(server):
                      1. alembic migrations (per data-dir DB)
                      2. read_only_replica.sync
                      3. background: backup_cron + importance_scheduler
                      4. periodic loop (15-min tick)  ← see §2
                                 │
   CODE REGISTRY ────────────────┤  base.register_layer(name, LayerBinding)
   (new layers)                  │  server.PRIMITIVE_TOOLS + ARIEL_EXPOSE
                                 └──────────────────────────────────────────────
```

## 2. Background loops (guaranteed housekeeping)

| Loop | Interval | Does |
|---|---|---|
| `backup_cron` | backup_interval_hours=24 ± jitter 3600s | DB backups, retention cleanup; fires `nightly` hook; wiki sync every wiki_sync_interval_minutes=30 |
| `importance_scheduler` | internal cadence | importance decay scheduling |
| periodic tick | 15 min | `forgetting_system.cleanup()` |
| ↳ compaction | hourly | `run_cleanup(user_id="default")` — archive/decay |
| ↳ consolidation sweep | hourly | `consolidate_episodes("default")` per layer (user+agent): high-weight episodes → L4 facts |

All loops run inside the SERVER process — housekeeping never depends on an agent calling tools.

## 3. Event hooks (reactive loop)

Tool calls fire named hooks (`_fire_hook`) → `hook_registry.fire` → **gate: `config.is_hook_enabled(layer, hook)`** → AgentHooks/UserHooks handlers.

Fired from: episodic save (`consolidation`), session end (`consolidation`), think (`message_received`, `emotion_trigger`), dream (`auto_context`, `dream_buffer`, `retrieval_router`), evolve (`personality_shift`), remember (`state_delta`, `conflict_resolver`, `error_occurred`, `decision_made`...).

Known noise: consolidation handler reads `staging_items` from ctx which no caller provides → effectively no-op (real promotion lives in the hourly sweep, see §2).

## 4. config.yaml — section audit

| Section | Status | Consumer / note |
|---|---|---|
| `layers.*.enabled` | ☠️ DEAD | zero readers |
| `limits.l1_buffer_size` | ✅ LIVE | ReflexBuffer via get_limit; l2/l3/l4 limits unread |
| `hooks.user/agent.*` | ✅ LIVE | is_hook_enabled; but code ALSO duplicates lists (`known_hooks`) — drift risk, unknown hooks default False |
| `forgetting.decay_rate / archive_threshold_days / archive_min_importance` | ✅ LIVE | ForgettingSystem |
| `forgetting.consolidate_weight_threshold` | ☠️ DEAD | hardcoded min_weight=0.7 in consolidate_episodes |
| `rag.*` | ☠️ DEAD | RAGEngine uses constructor defaults; no config readers |
| `typed_memory.*` | ☠️ DEAD | policies hardcoded in shared/memory_types.py; TTL 30d hardcoded in CoreMemory._prepare_save_params |
| `binary.*` | ☠️ DEAD | dim=384/mode=naive hardcoded defaults |
| `embeddings.*` | ☠️ DEAD | DEFAULT_MODEL hardcoded multilingual-e5-small in shared/embeddings.py |
| `graph.max_depth` | ✅ LIVE | EpistemicGraph |
| `graph.temporal_enabled/epistemic_enabled` | ☠️ DEAD | zero readers |
| `wiki.<layer>` type flags + external_dirs | ✅ LIVE | wiki/shared.get_external_dirs + enabled-types check |
| `auth.bearer_token_enabled` | ✅ LIVE | endpoints/common |
| `auth.api_keys_enabled` | ☠️ DEAD | zero readers (API keys feature self-managed) |
| `features.rate_limiting` | ✅ LIVE | rate_limiting feature gate |
| `features.backup_cron` | ⚠️ INDIRECT | gate is env BACKUP_CRON_DISABLED, yaml flag unread |
| `features.dashboard` vs `dashboard.enabled` | ☠️ DEAD ×2 | server uses --dashboard CLI flag only; two yaml flags contradict each other and neither is read |
| `features.*` rest (import_export, versioning, cross_user_learning, compression, webhooks, audit_trail, auth, embeddings) | ☠️ DEAD | is_feature_enabled has NO callers at all |
| `performance.*` | ☠️ DEAD | WAL hardcoded ON in connection.py; pool/cache sizes unread |
| `security.rate_limit_per_user / max_ws_per_user / max_ws_total` | ✅ LIVE | RateLimiter / middlewares (max_ws_* questionable: no WebSocket surface in stdio mode) |
| `security.input_validation / sql_injection_prevention / per_user_isolation / audit_logging` | ☠️ DECORATIVE | zero readers |
| `backup.interval/jitter/wiki_sync/retention` | ✅ LIVE | backup_cron |
| `dashboard.port` | ☠️ DEAD | argparse --port default 8000 |
| `logging.*` | ☠️ DEAD | zero readers |
| *(not in yaml)* `cors.allowed_origins` | ✅ LIVE | middlewares read it with localhost defaults — consider adding to yaml |
| *(not in yaml)* `crypto.master_key_hex` | ⚠️ fallback | secrets.py chain: .env file → crypto.master_key_hex → env MCP_MASTER_KEY |

**Score: of ~60 yaml keys only ~15 are live.** The dead weight is v1-era aspiration config kept after refactors.

## 5. Gotchas

1. **config.yaml is repo-root-relative** (`Path(__file__).parent`) — all three agent instances (hermes/mimocode/cowagent) share ONE config even though their DATA dirs differ. Changing hooks flags affects every agent.
2. Hook gating double-default: known hooks default True, unknown default False — adding a new hook handler requires remembering to add it to BOTH yaml and `known_hooks`.
3. `get_wiki_types()` in config.py expects a list and returns [] on the actual dict-shaped config — misleading dead method (real mechanism: wiki/shared.py).

## 6. Recommendations

1. Delete or implement dead sections (biggest: rag/embeddings/binary/typed_memory/performance/logging/dashboard).
2. Single source for hooks: drop `known_hooks` duplication, make yaml explicit.
3. Add `cors:` to yaml explicitly.
4. Resolve dashboard triple-switch (keep CLI flag only).
5. If multi-instance divergence ever needed: move config path resolution to `MCP_CONFIG_PATH` env override.
