# Auto-Hooks — the push-model memory keystone

> Phase C of the roadmap. **Push model**: ariel observes, evaluates, saves and
> injects on its own — the agent does not have to call memory tools to remember.

## The problem

Ariel hooks fire when the agent calls an ariel tool. The agent's conversation
with the user is **not** an ariel tool call — so hooks never see it, and
memory only happens when the agent remembers to remember. Auto-hooks close
that gap with a harness-side observer + a server-side event pipeline.

## Architecture

```
conversation store (SQLite)                     agent's ariel instance (stdio)
  Hermes state.db / Cow index.db /                ┌──────────────────────────┐
  MiMoCode part table                             │ hooks/external.py        │
        │                                         │  dispatch_event(event)   │
        ▼                                         │  → HookRegistry.fire     │
  ┌───────────────────┐   new_message event       │  → handlers do the work  │
  │ autohooks daemon  │ ────────────────────────► │    L3 / graph / L4       │
  │ (per-agent, 15s)  │   session_started ──────► │  → memory_dispatch_log   │
  └───────────────────┘   post_session_diff ───► └──────────────────────────┘
        ▲                                                     │
        │ session.start / session.end (harness hook)          ▼
  harness hook dir / plugin                        build_inject_blocks → agent
                                                   sees critical set + pending
                                                   proposals on wake
```

Two transports, one dispatcher (`hooks/external.py::dispatch_event`):
`POST /api/hooks/{event}` (HTTP mode) and the `memory_hook` MCP tool +
in-process `autohooks dispatch` (stdio mode). Isolation is inherited: each
agent runs its own ariel instance with its own `MCP_MEMORY_DATA_DIR`.

## Events (KNOWN_EVENTS)

| Event | Fired by | Day-one handler |
|---|---|---|
| `session_started` | harness session start / inject CLI | returns the budget-capped critical set |
| `session_ended` | harness session end | saves `payload.summary` to L3 |
| `new_message` | autohooks daemon | `evaluate_importance` → threshold-gated saves |
| `auto_save_candidate` | daemon/harness | same pipeline as `new_message` |
| `post_context_compression` | harness (compaction boundary) | drift log row + rehydrate candidates via retrieval |
| `context_threshold` | harness | thin advice: eviction candidates |
| `memory_pressure` | harness | thin advice: compression candidates |
| `post_session_diff` | harness session end (C1.10) | materializes save gaps as L3 `diff_gap` episodes |

## Staged mutation (proposal → review → apply)

Risk-tier **true staging**: L4-destined writes (auto-save score ≥ 0.8,
consolidation promotions, forgetting-ritual archives) become
`mutation_proposals` rows instead of writing immediately. L3 episodes, graph
nodes, importance boosts and explicit agent tool calls stay direct.

- Pending proposals surface in the session-start inject as a `proposals`
  block — the decision is one `memory_proposals` tool call.
- `action="decide"` applies (executes the exact write the direct path would
  have) or rejects; `action="revert"` undoes an **applied** proposal with
  exact provenance (core_write, archive).
- 7-day lazy expiry (no cron); every decision is audit-logged.
- `staging.enabled = false` restores direct behavior entirely.

## Dream markers

`DREAM: memory: …` / `DREAM: fact: …` / `DREAM: skill: …` in any conversation
message bypass the importance heuristic and go through staging at importance
0.95 (`skill:` also writes an L3 `dream_skill` episode for the future skill
store). Toggle: `staging.dream_markers` (default `true`).

## Compaction rehydrate (D3.5)

Compaction-aware memory: ariel learns that an agent's context was compacted
and rehydrates the critical set instead of silently losing it.

- **Drift log** — every `post_context_compression` dispatch writes a
  `compaction_events` row (user, old/new session ids, reason, summary).
- **MiMoCode** — the `ariel-inject` plugin wires real fork hooks:
  `experimental.session.compacting` appends the ariel critical set to the
  summarizer prompt (salvage); the `session.compacted` event dispatches the
  drift and arms a one-shot `[ariel rehydrate]` block delivered via
  `experimental.chat.system.transform`; `session.post` fires the debounced
  `post_session_diff`.
- **Hermes** — the gateway emits a `compaction` event at the compression
  boundary; the `ariel-compaction` hook dir dispatches it to ariel. Because
  Hermes rotates the session id on compression, the rotated session's
  session-start inject carries the rehydrate block.
- **Inject block** — `build_inject_blocks` emits a `rehydrate` block (important
  L4 facts, score 0.9) when a compaction happened within
  `rehydrate.window_hours` (default 6h); `rehydrate.enabled = false` turns it
  off. `autohooks inject --blocks rehydrate` renders just this block.
- `dispatch --payload '{"old_session_id": …, "reason": …}'` passes event
  context through the CLI.

## The autohooks runtime

`python -m autohooks` (package `autohooks/`) — per-agent daemon + inject CLI.
See **[Connecting autohooks to other platforms](autohooks-platforms.md)** for
the full wiring guide, config schema and troubleshooting.

## Observability

- `memory_dispatch_log` — one row per save path (substrate for gaps).
- `compaction_events` — one row per detected compaction (drift log for rehydrate).
- `memory_watch` — operator CRUD over the rules ariel applies.
- `memory_report_card(period_hours=24)` — digest: proposal decisions,
  save-tier sums, open gaps, dream markers.
- `scripts/verify_autohooks.py` — end-to-end verification (24 checks).

## Config

All knobs and their defaults: see the
[platform guide's config reference](autohooks-platforms.md#config-reference)
and `docs/CONTROL_MAP.md`.
