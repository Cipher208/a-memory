# Hooks System

## Overview

24 hooks (12 user + 12 agent) intercept memory operations at every stage.

## Hook Types

| Hook | Trigger | Purpose |
|------|---------|---------|
| `before_remember` | Before storing | Filter/modify content |
| `after_remember` | After storing | Side effects (notifications) |
| `before_recall` | Before search | Modify query |
| `after_recall` | After search | Post-process results |
| `before_forget` | Before delete | Archive check |
| `after_forget` | After delete | Cleanup |
| `before_session` | Before session op | Validate |
| `after_session` | After session op | Sync |
| `before_graph` | Before graph op | Validate |
| `after_graph` | After graph op | Index |
| `before_wiki` | Before wiki op | Validate |
| `after_wiki` | After wiki op | Sync |

## Importance Gate

Hooks use `ImportanceGateMiddleware` to filter low-importance content:

```python
# Default threshold: 0.3
# Content with score < 0.3 is filtered out
# Score is computed by ImportanceScorer (8 signals)
```

## Custom Hooks

```python
from hooks.registry import register_hook


@register_hook("before_remember", layer="user")
async def my_hook(ctx):
    if "spam" in ctx.content.lower():
        ctx.cancel = True
    return ctx
```
