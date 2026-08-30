# Connecting autohooks to other platforms — the full guide

> a-memory runs next to any agent that keeps its conversation in SQLite. This
> guide covers the universal recipe: the runtime, the per-agent config, the
> harness wiring, deployment, verification and troubleshooting. Live examples:
> Hermes, MiMoCode, CowAgent — three platforms already wired this way.

## 0. What you need

- A running a-memory install (this repo) with its venv: `uv sync --extra dev`.
- The agent's conversation store as **SQLite** (any table with a monotonic
  cursor column, a role-ish field and a text field — JSONB payloads are fine
  via `json_extract`).
- The agent's ariel MCP instance already provisioned (it owns a data dir with
  `memory.db` + `config.yaml`).

## 1. Architecture in one minute

```
agent conversation DB ──(poll)──► autohooks daemon ──(in-process)──► dispatch_event()
                                                                      │
                                                        ┌─────────────┴─────────────┐
                                                        ▼                           ▼
                                                 hook handlers                dispatch log
                                                 (L3/graph/L4 saves,          (gap substrate)
                                                  importance thresholds)
```

- **The daemon is a tailer, not a brain.** It polls the conversation DB every
  `poll_seconds`, and for each new message fires `new_message` **in-process**
  (imports ariel; no HTTP needed — ariel stdio mode has no HTTP surface).
- **The brain is server-side.** `evaluate_importance`, threshold-gated saves,
  staged mutations — all live in ariel and are shared by every platform.
- **Isolation is inherited.** The daemon sets `MCP_MEMORY_DATA_DIR` from its
  config before importing ariel; SQLite WAL makes multi-process access safe.
- **Triggers, not timers.** The poll is only transport; saves happen only when
  the server-side importance logic fires.

Two CLI subcommands and one event dispatcher:

| Command | What it does |
|---|---|
| `python -m autohooks daemon --config X.yaml` | tail loop → `new_message` events |
| `python -m autohooks inject --config X.yaml [--text S] [--format md\|json]` | session-start critical set on stdout |
| `python -m autohooks dispatch --config X.yaml --event EV [--since T --until T]` | fire any `KNOWN_EVENTS` event (harness-side trigger) |

## 2. Agent config reference (`~/.config/ariel-autohooks/<agent>.yaml`)

```yaml
data_dir: ~/.mcp-ariel-memory-<agent>   # → MCP_MEMORY_DATA_DIR (must exist)
user_id: default                        # ariel user inside the agent's instance
layer: user                             # "user" | "agent"
source:
  driver: sqlite                        # v1: the only driver
  path: ~/.hermes/state.db              # conversation DB (read-only URI)
  table: messages
  cursor_column: id                     # int PK/rowid → incremental cursor
  order_by: id
  role: {column: role}                  # or {json_path: [data, '$.role']}
  text: {column: content}               # or {json_path: [data, '$.text']}
  ts: {column: timestamp}               # optional
  filter: "role IN ('user', 'assistant')"   # optional WHERE fragment
poll_seconds: 15
batch_limit: 100
state_file: <data_dir>/autohooks-cursor.json   # default; overrideable
master_key: <hex>                       # optional; else MCP_MASTER_KEY env
```

Rules: unknown keys are hard errors; exactly one of `column` / `json_path`
per field; `~` expands. Examples for real platforms live in
`autohooks/examples/` — **copy the closest one and change paths**.

First-run behavior: cursor = `max(cursor_column)` — **no history replay**;
only messages arriving after daemon start are evaluated. At-least-once
delivery: the cursor persists after each batch, a crash may re-dispatch at
most one batch.

## 3. Universal wiring recipe (any platform)

1. **Write the config** (§2) pointing at the platform's conversation DB.
2. **Deploy the daemon** — always-on systemd `--user` unit is the recommended
   shape (the daemon is a plain tailer; it does not need session lifetime):

   ```ini
   # ~/.config/systemd/user/ariel-autohooks-<agent>.service
   [Unit]
   Description=ariel autohooks daemon (<agent>)
   After=default.target

   [Service]
   Type=simple
   Environment=ARIEL_HASH_EMBEDDINGS=1
   ExecStart=/home/<you>/mcp-ariel-memory/.venv/bin/python3 -m autohooks daemon \
       --config /home/<you>/.config/ariel-autohooks/<agent>.yaml
   WorkingDirectory=/home/<you>/mcp-ariel-memory
   Restart=on-failure
   RestartSec=10

   [Install]
   WantedBy=default.target
   ```

   ```bash
   systemctl --user daemon-reload && systemctl --user enable --now ariel-autohooks-<agent>
   ```

3. **Session-start inject** — wire the platform's session-start hook to:
   `python -m autohooks inject --config <agent>.yaml` and embed stdout into
   the agent's system context (write a prompt file, call `addContext`, append
   to a bootstrap prompt — whatever the platform offers). Empty critical set
   prints `—` (skip it).
4. **Session-end diff (recommended)** — wire the session-end hook to:
   `python -m autohooks dispatch --config <agent>.yaml --event post_session_diff
   --since <start> --until <now>`. Gaps become L3 `diff_gap` episodes and ride
   the next inject automatically.
5. **Verify** (§6).

### 3.1 Platform notes

| Platform | Conversation source | Session hooks | Status |
|---|---|---|---|
| **Hermes** | `~/.hermes/state.db` → `messages(id, session_id, role, content, timestamp)`; filter `role IN ('user','assistant')` — daemon only; turn sync rides the native provider | **native `MemoryProvider` plugin** `~/.hermes/plugins/ariel/` (`memory.provider: ariel` in config.yaml): `system_prompt_block` = critical set, `sync_turn` = per-turn push (replaces the daemon's role for turn capture), `on_pre_compress` = drift + salvage, `on_session_end` = diff, `on_memory_write` = MEMORY.md mirror, `queue_prefetch`/`prefetch` = per-turn recall. Pure-stdlib over the ariel venv CLI — survives `hermes update` venv rebuilds. Legacy file hooks (`~/.hermes/hooks/ariel-autohooks`, `ariel-compaction`) moved to `hooks.disabled/`; the two ariel core commits (gateway emit, prompt slot) were reset back to upstream | live |
| **MiMoCode** | `~/.local/share/mimocode/mimocode.db` → `part(rowid, message_id, data JSON)`; role via subquery on `message.data`, text via `json_extract(data,'$.text')`, filter `json_extract(data,'$.type')='text'` | `~/.config/mimocode/hooks/ariel-inject.ts` on REAL fork hook keys: `experimental.chat.system.transform` (one-shot critical set + post-compaction `[ariel rehydrate]`), `experimental.session.compacting` (salvage into the summarizer prompt), `event` on `session.compacted` (drift dispatch), `session.post` (debounced `post_session_diff`) | live |
| **CowAgent** (persona Eli) | `~/cow/memory/long-term/index.db` → `messages(id, session_id, seq, role, content, created_at)` | code-level: `MemoryManager.flush_memory` fires compaction drift (overflow/trim only) via ariel CLI; `agent_stream` appends a one-shot `[ariel rehydrate]` system message after trim/smart-compaction (excluded from flush by the manager filter). No plugin-bus lifecycle events exist upstream | live |
| **Any SQLite platform** | §2 schema | §3 recipe | recipe |

Naming note: the `eli` data dir is the pre-migration home of Cow's memory —
the live dir for Cow is `~/.mcp-ariel-memory-cowagent` (both carry the eli
wiki; the legacy dir is kept for history). Three agents are wired today:
Hermes, MiMoCode, CowAgent. The bare `~/.mcp-ariel-memory` dir is dev/legacy
(no live config points at it).

## 3.2 Runtime components — daemons, plugins, scripts

### Daemons (systemd --user, always on)

```
ariel-autohooks-hermes.service     ariel-autohooks-mimocode.service     ariel-autohooks-cowagent.service
```

Each runs `python -m autohooks daemon --config ~/.config/ariel-autohooks/<agent>.yaml`:
tails the agent's conversation SQLite (read-only URI, first-run baseline =
max id, at-least-once), pushes `new_message` into the in-process dispatcher.
Restart after ariel code updates: `systemctl --user restart ariel-autohooks-*`.
CowAgent additionally runs under a **system** unit `cowagent.service`
(Restart=always) — the agent itself, not the daemon; killing its PID
auto-respawns it with new code (do NOT manual-nohup: `-m app` needs
cwd=/home/murat/cowagent).

### Hermes — native `MemoryProvider` plugin (preferred path)

`~/.hermes/plugins/ariel/` (`plugin.yaml` + `__init__.py`), activated by
`memory.provider: ariel` in `~/.hermes/config.yaml`. Pure-stdlib over the
ariel venv CLI — `hermes update` venv rebuilds cannot break it.

| Provider hook | What it does | Replaces |
|---|---|---|
| `system_prompt_block()` | critical set at session start (cached) | session-start inject file hook |
| `queue_prefetch` / `prefetch(query)` | per-turn **/recall protocol** (D1.1), cache pattern | — (new capability) |
| `sync_turn(user, assistant)` | per-turn push → auto-save pipeline | conversation tailing for turn capture |
| `on_pre_compress(messages)` | drift log + critical set into the summary prompt (salvage) | gateway compaction emit |
| `on_session_end` | `post_session_diff` (24h window) | session:end file hook |
| `on_memory_write` | mirrors MEMORY.md writes → auto-save | — |
| `on_session_switch` | cache invalidation on reset/compression | — |

The plugin runs every CLI call on one background worker thread; failures are
visible in `/tmp/ariel-provider.log`. The ariel MCP server stays separate
(41+ tools via stdio). The legacy file hooks were moved to
`~/.hermes/hooks.disabled/` and the two ariel core commits (gateway emit,
prompt slot) were reset to upstream — the plugin is the only integration.
In-process ariel import is the documented v2 (needs aiosqlite pinned in the
Hermes venv — volatility risk).

### MiMoCode — fork-hooks plugin

`~/.config/mimocode/hooks/ariel-inject.ts` on REAL fork hook keys
(`experimental.chat.system.transform`, `experimental.session.compacting`,
`event` on `session.compacted`, `session.post`). Survives fork updates
(outside the repo); re-run the bun smoke after fork updates — it depends on
experimental hook key names.

### CowAgent — code-level hooks

`agent/memory/manager.py::flush_memory` fires the compaction drift
(overflow/trim only) and `agent/protocol/agent_stream.py` appends the
one-shot `[ariel rehydrate]` system message after trim/smart-compaction.
No plugin-bus lifecycle events exist upstream; these are the only dispatch
points. `flush_memory` excludes `[ariel rehydrate]` messages from the flush
input (feedback-loop guard).

### Scripts

| Script | Purpose |
|---|---|
| `scripts/verify_autohooks.py` | end-to-end ariel-side verification (24 checks) on a scratch data dir |
| `scripts/sync_skills.py` | shared skill SSOT (`~/skills-ssot`, git) → all agent wikis; `--bootstrap` creates the skeleton; hash-dedup makes repeats no-ops |

Both close ariel's aiosqlite connections before exit — without that the
non-daemon worker threads hang the interpreter AFTER printing (mask nothing:
judge outputs, not exit codes through pipes).


## 4. Config reference — every knob in one table

### ariel `config.yaml` (per data dir; `MCP_CONFIG_PATH`)

| Key | Default | Controls |
|---|---|---|
| `hooks.auto_save_threshold` | `0.5` | score ≥ → L3 episode + graph node |
| `hooks.<layer>.<hook>` | `true` | per-hook enable flags (`is_hook_enabled`; unknown hooks default on) |
| `inject.token_budget` | `2000` | critical-set token cap |
| `inject.important_min` | `0.8` | L4 facts included in the inject block |
| `staging.enabled` | `true` | risk-tier staging (L4 saves, consolidation promos, archive sweeps → proposals); `false` = legacy direct writes |
| `staging.expire_days` | `7` | proposal expiry (lazy, no cron) |
| `staging.dream_markers` | `true` | `DREAM:` marker detection; `false` = markers take the heuristic path |

### Per-agent autohooks YAML (§2)

`data_dir`, `user_id`, `layer`, `source.*` (driver/table/cursor/mappings/filter),
`poll_seconds`, `batch_limit`, `state_file`, `master_key`.

### Environment

| Var | Effect |
|---|---|
| `MCP_MEMORY_DATA_DIR` | data dir (isolation boundary) — set by the YAML |
| `MCP_CONFIG_PATH` | per-agent ariel config.yaml |
| `MCP_MASTER_KEY` | crypto master key (or `master_key:` in the YAML) |
| `ARIEL_HASH_EMBEDDINGS=1` | deterministic hash embeddings — **set it in every unit/hook**; the real sentence-transformers model adds >15 s per CLI process |
| `ARIEL_EXPOSE` | tool surface: `primitives` (default, 6 tools), `primitives,review` (adds `memory_proposals` + `memory_report_card` for agent-side review), `all` |

### Who decides what (mental model)

- **What gets saved** → ariel config (`hooks.auto_save_threshold`, staging
  policy) — one place for all platforms.
- **What gets observed** → the per-agent YAML (source driver) + harness hooks
  (session start/end triggers).
- **What the agent sees** → `inject.token_budget` + pending proposals;
  review rights → `ARIEL_EXPOSE` tier.

## 5. Data flow guarantees

- **At-least-once** event delivery (crash → re-dispatch ≤ 1 batch).
- **Every save is logged** (`memory_dispatch_log`) — the diff/report substrate.
- **Risky writes wait**: L4-tier automation lands in `mutation_proposals`
  first; `memory_proposals` decides; `revert` undoes with exact provenance.
- **Gaps are surfaced, not lost**: dispatch-vs-persisted mismatches become
  `diff_gap` L3 episodes → next inject.

## 6. Verification

```bash
# end-to-end audit on a scratch dir (24 checks: registry, 8 events, daemon tail,
# staging lifecycle, markers, inject blocks, diff chain, report card,
# consolidation/forgetting routing, HTTP surfaces):
ARIEL_HASH_EMBEDDINGS=1 uv run --with-editable . --extra dev python scripts/verify_autohooks.py

# live instance: report card (CLI form; ctx-free)
MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory-<agent> \
MCP_CONFIG_PATH=~/.mcp-ariel-memory-<agent>/config.yaml \
ARIEL_HASH_EMBEDDINGS=1 \
  uv run --with-editable . --extra dev python -m autohooks inject --config ~/.config/ariel-autohooks/<agent>.yaml
```

Expected: `24/24 checks passed — ALL GREEN`; inject prints markdown blocks or
`—` and **exits** (see troubleshooting #3).

## 7. Troubleshooting (battle-tested)

1. **inject hangs >15 s and prints nothing** — the sentence-transformers model
   is loading. Set `ARIEL_HASH_EMBEDDINGS=1` in the unit/hook env. (Also:
   exit codes were masked by pipes — judge `cmd > file`, never `cmd | tail`.)
2. **CLI prints output then hangs at exit** — ariel's aiosqlite worker is a
   non-daemon thread; the CLI calls `connection_manager.close_all()` before
   exit since C1.10. If you embed the runtime, do the same.
3. **`alembic upgrade head` touches the wrong file** — fixed: `alembic.ini`
   no longer carries a CWD-relative `sqlalchemy.url`; env.py resolves the DB
   from `MCP_MEMORY_DATA_DIR`. Programmatic callers (`MigrationManager`) set
   the URL explicitly. Symptom of the old bug: a `memory.db` appearing in the
   repo root / CWD.
4. **Daemon starts but nothing is saved** — check the cursor baseline: first
   run starts at `max(id)` (no replay). If the conversation DB was seeded
   before daemon start, those rows are intentionally skipped.
5. **`no such table: mutation_proposals`** — run the migrations on the data
   dir: `MCP_MEMORY_DATA_DIR=<dir> alembic upgrade head` (env.py now honors
   it), or `alembic stamp` + direct SQL per the C1.10/C1.11 notes.
6. **Proposals pile up** — nobody is reviewing. Either review via
   `memory_proposals` (operator or agent with the `review` tier), or let the
   7-day expiry reject them, or set `staging.enabled=false`.
7. **Tests / CI** — full gate: `ruff check . && ruff format --check . && mypy
   features/ shared/ mcp_server/ rag/ hooks/ wiki/ lifecycle/ graph/ core/
   autohooks/ && pytest tests/ -q`; each command separately, never through
   pipes.
