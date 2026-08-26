from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002 — runtime: MCPServer evaluates this annotation at registration

from mcp_server.models import ThinkResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from mcp_server.tools.base import (
    _validate_layer,
    _check_rate_limit,
    _get_memory,
    _get_graph,
    _get_wiki,
    _fire_hook,
)
from mcp_server.tools.primitives.routing import _auto_route

from mcp_server.context import AppContext  # noqa: TC001 — runtime: MCPServer evaluates this annotation at registration

logger = logging.getLogger(__name__)


async def think(
    text: str,
    layer: Literal["user", "agent", "auto"] = "auto",
    user_id: str = "default",
    wiki_type: str | None = None,
    wiki_title: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: routing thoughts to correct memory layers based on importance and content."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_think")

    # 1. Rate limiting
    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return dict(rate_limit)

    # 2. Importance Scoring
    scorer_result = app.importance.score(text)
    importance = scorer_result.score

    # 3. Layer Resolution
    resolved_layer: str = layer
    if layer == "auto":
        resolved_layer = _auto_route(text)

    _validate_layer(resolved_layer)

    # 4. Routing Logic
    actions = []
    routing = {"importance": importance, "length": len(text), "emotional_weight": scorer_result.signals.emotional, "resolved_layer": resolved_layer}

    mem = _get_memory(app, resolved_layer, user_id)
    graph = _get_graph(app, resolved_layer)
    wiki = _get_wiki(app, resolved_layer)

    tasks = []

    forced_wiki = bool(wiki_type or wiki_title)
    large_text = len(text) > 2000

    if forced_wiki or large_text:
        w_type = wiki_type or ("decision_log" if resolved_layer == "agent" else "diary")
        title = wiki_title or f"Thought_{int(time.time())}"
        wiki_path = await wiki.add(wiki_type=w_type, title=title, content=text)

        summary = text[:200] + "..."
        text_to_save = f"Summary: {summary} | Path: {wiki_path}"
        action_type = "Wiki_save" if forced_wiki else "Wiki_thought_save"
        actions.append({"type": action_type, "path": wiki_path})

        # Also save summary/link to memory so dream() can find the page
        if importance > 0.7:
            tasks.append(mem.remember("thought_link", text_to_save, importance))
            actions.append({"type": "L4_remember_link", "importance": str(importance)})
        else:
            tasks.append(mem.l3.save(user_id, text_to_save, float(scorer_result.signals.emotional)))
            actions.append({"type": "L3_episodic_save_link", "weight": str(scorer_result.signals.emotional)})
    else:
        # Standard routing
        # If len(text) < 60 and importance is high -> Save to CoreMemory (L4)
        if len(text) < 60 and importance > 0.7:
            tasks.append(mem.remember("thought", text, importance))
            actions.append({"type": "L4_remember", "importance": str(importance)})

        # If len(text) >= 60 or emotional weight is detected -> Save to Episodic (L3)
        if len(text) >= 60 or scorer_result.signals.emotional > 0.5:
            tasks.append(mem.l3.save(user_id, text, float(scorer_result.signals.emotional)))
            actions.append({"type": "L3_episodic_save", "weight": str(scorer_result.signals.emotional)})

        # Fallback: a write primitive must never silently drop content that
        # matched neither the L4 nor the L3 rule.
        if not any(a["type"].startswith(("L4_", "L3_", "Wiki_")) for a in actions):
            tasks.append(mem.l3.save(user_id, text, float(scorer_result.signals.emotional)))
            actions.append({"type": "L3_episodic_save_fallback", "weight": str(scorer_result.signals.emotional)})

    # Relation detection
    relation_patterns = [r"\b\w+\s+(is|related\s+to|connected\s+to|part\s+of)\s+\w+\b"]
    has_relation = any(re.search(p, text, re.IGNORECASE) for p in relation_patterns)

    if has_relation:
        tasks.append(graph.add_node(user_id, text[:500], "relation", ["think_primitive"], importance))
        actions.append({"type": "Graph_node_add", "node_type": "relation"})

    # 5. Hooks
    hook_tasks = [_fire_hook("message_received", resolved_layer, {"text": text, "user_id": user_id}, mem=mem)]
    # User-emotion analysis only makes sense on the user layer; agent
    # self-reflection runs through its own graph hooks instead.
    if resolved_layer == "user":
        hook_tasks.append(_fire_hook("emotion_trigger", resolved_layer, {"text": text, "user_id": user_id, "importance": importance}, mem=mem))

    # Timeline: significant thoughts become temporal events (never breaks the primitive)
    if app.temporal and actions:
        with contextlib.suppress(Exception):
            await app.temporal.add_event(
                user_id,
                "thought",
                text[:200],
                importance=float(importance),
                metadata={"resolved_layer": resolved_layer, "actions": len(actions)},
                layer=resolved_layer,
            )

    import inspect

    awaitable_tasks = [t for t in tasks + hook_tasks if inspect.isawaitable(t)]

    if awaitable_tasks:
        await asyncio.gather(*awaitable_tasks)

    return ThinkResult(status="ok", routing=routing, actions=actions).dict()
