---
type: "Reference"
title: "Lifecycle and hooks"
openwiki_generated: true
verified:
  - by: openwiki/0.6.0
    at: 2026-09-24T20:32:03.830Z
sources:
  - id: openwiki-source-4dfae4f0b9d754b8723f63d2
    resource: repo://autohooks/daemon.py
  - id: openwiki-source-d6fd35e7538e0e89ca2cf671
    resource: repo://graph/epistemic.py
  - id: openwiki-source-a3e66e1d7ebd161aa07d1586
    resource: repo://graph/temporal.py
  - id: openwiki-source-5eaa3a704bdf6bbb6c37e3ca
    resource: repo://hooks/registry.py
  - id: openwiki-source-739224de275c61771175b1e2
    resource: repo://lifecycle/graph_sanitation.py
generated: { by: "opencode", at: "2026-09-24T20:32:03.830Z" }
---

# Lifecycle and hooks

Memory in this repo is event-driven: hook handlers react to session events,
a poll daemon feeds them, and scheduled lifecycle passes consolidate, distill,
forget, and repair the stored knowledge.

## Hook dispatch (`hooks/`)

- `hooks/registry.py` owns `HookRegistry` and `HookHandler` (a pydantic model)
  plus `dispatch_event` — the single entry point for fanning an event out to
  handlers — and `auto_save_text` for persisting candidate text.
- Hooks are split by layer: `hooks/agent_hooks.py` vs `hooks/user_hooks.py`,
  with `hooks/shared.py` for common helpers and `hooks/models.py` for payload
  shapes. `hooks/loader.py` exposes `load_all_hooks()`.
- `hooks/external.py` bridges events that originate outside the MCP server
  process.

## Autohooks daemon (`autohooks/`)

- `autohooks/daemon.py` runs a poll loop dispatching `new_message` events
  (spec S4). The poll is only transport — saves happen server-side inside
  `dispatch_event` handlers once `evaluate_importance` crosses threshold
  ("triggers, not timers").
- Delivery is at-least-once: the SQLite cursor persists after each batch, so a
  crash re-dispatches at most one batch.
- The daemon also drives periodic graph hygiene: the central dangling-edge
  prune (`lifecycle.graph_sanitation.prune_dangling_edges`) runs at most every
  600 s from the daemon loop.
- `autohooks/inject.py` renders memory blocks to markdown for prompt
  injection; `config.py`, `context.py`, `source.py`, and `appctx.py` carry
  agent configuration and source plumbing.

## Lifecycle passes (`lifecycle/`)

- Consolidation and distillation: `consolidation.py`, `segment_consolidation.py`,
  `distiller.py`; forgetting pipeline in `forgetting.py`.
- Scheduling and triage: `importance_scheduler.py`, `l0_tiers.py`, `l0_sweep.py`,
  `gap_registry.py`, `transitions.py`, `tool_stats.py`, `qfields.py`,
  `compact.py`.
- Graph upkeep lives here too: `graph_builder.py`, `graph_miners.py`,
  `graph_enrich.py`, `graph_sanitation.py`, `wiki_graph_builder.py`,
  `wiki_communities.py`, and `graph_mermaid.py` for diagram export.
- `lifecycle/emotion/` holds emotion-scored processing.

## Temporal and epistemic graph (`graph/`)

- `graph/temporal.py` tracks time-scoped relations; `graph/epistemic.py`
  tracks belief/confidence structure over memorised items.
