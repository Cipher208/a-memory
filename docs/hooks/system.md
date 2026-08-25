# Hooks System

## Overview

Lifecycle hooks intercept memory operations at every stage. Config defines **12 user-layer and 13 agent-layer hook slots** (19 unique names); each is toggled per layer under `hooks.user.*` / `hooks.agent.*` in `config.yaml`. A missing key means enabled.

## Hook names

| Hook | Layer(s) | Fires on |
|------|----------|----------|
| `message_received` | both | content stored via `think` / `memory_remember`, session start |
| `message_sent` | user | outbound message events |
| `state_delta` | both | episode save, session end |
| `consolidation` | both | episode save, session end, hourly sweep |
| `emotion_trigger` | both | emotional content detected in `think` / saves |
| `importance_gate` | user | before `memory_remember` commits — can skip low-importance saves |
| `auto_context` | both | after `dream` / `recall` / context injection |
| `dream_buffer` | both | `dream` digest and context injection staged into the DreamBuffer |
| `retrieval_router` | both | before search operations |
| `conflict_resolver` | both | conflicting facts detected |
| `forgetting_ritual` | both | forgetting cycles |
| `nightly` | user | nightly maintenance |
| `error_occurred` | agent | error-keyed memories / error-analysis graph nodes |
| `decision_made` | agent | decision-keyed memories / decision-log nodes |
| `self_correction` | agent | correction-keyed memories |
| `personality_shift` | agent | `evolve` calls, personality graph nodes |
| `emotion_context` | agent | emotion-typed graph nodes |
| `wiki_agent` | agent | wiki lookups during context injection |
| `emotion` | agent | emotion events |

## Importance Gate

The `importance_gate` hook filters low-importance content before it reaches L4. Scores come from `ImportanceScorer`; the acceptance threshold adapts via an EMA of recent activity rather than staying fixed — busy periods raise the bar. Skipped saves return `{"status": "skipped", "reason": "below_importance_threshold"}`.

## Custom Hooks

```python
from hooks import hook_registry


class MyHooks:
    @hook_registry.mark("error_occurred", layer="agent")
    async def on_error(self, ctx: dict, mem=None) -> dict:
        # ctx: event payload (e.g. {"key": ..., "value": ..., "user_id": ...})
        # mem: optional layer store handle
        return {"summary": "error noted"}


hook_registry.register_instance(MyHooks())
```

Contract:

- Handlers touching stores MUST be `async def`.
- The optional `mem` parameter receives the layer's memory handle.
- Registration happens through `register_instance` (wired for the built-in user/agent hooks during app startup).
