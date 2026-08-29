"""External event dispatcher — one entry point, two transports (HTTP + MCP tool).

Harnesses (Hermes/MiMoCode/CowAgent) push lifecycle events; ariel-side handlers
do the in-server work. Isolation is inherited: each agent runs its own ariel
instance (own process + MCP_MEMORY_DATA_DIR).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "session_started",
        "session_ended",
        "new_message",
        "auto_save_candidate",
        "context_threshold",
        "memory_pressure",
        "post_context_compression",
    }
)


async def dispatch_event(app: Any, event: str, layer: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate + fire one external event. Raises ValueError on unknown event."""
    if event not in KNOWN_EVENTS:
        raise ValueError(f"unknown event: {event!r}. Must be one of {sorted(KNOWN_EVENTS)}")
    from mcp_server.tools.base import _get_graph, _get_memory
    from shared.metrics import metrics

    metrics.inc(f"hook_event_{event}")
    layer = (layer or "user").strip().lower()
    if layer not in ("user", "agent"):
        raise ValueError(f"invalid layer: {layer!r}")
    mem = _get_memory(app, layer, user_id)
    graph = _get_graph(app, layer)
    context: dict[str, Any] = {"user_id": user_id, "_app": app, **payload}
    try:
        from mcp_server.tools.base import _get_rag

        context["_rag"] = _get_rag(app, layer)
    except Exception as exc:
        logger.debug("rag resolution for external event failed: %s", exc)
    from hooks.registry import hook_registry

    return await hook_registry.fire(event, layer, context, mem=mem, graph=graph)


async def auto_save_text(mem: Any, graph: Any, user_id: str, text: str) -> dict[str, Any]:
    """evaluate_importance → threshold-gated saves. Shared by new_message + auto_save_candidate.

    score >= hooks.auto_save_threshold (default 0.5) → L3 episodic + graph node;
    score >= 0.8 → also L4 core. Never raises past the caller (fire catches).
    """
    from config import config

    from features.importance import evaluate_importance

    score = evaluate_importance(text)
    result: dict[str, Any] = {"score": score, "saved_l3": False, "saved_l4": False, "saved_graph": False}
    threshold = float(config.get("hooks", "auto_save_threshold", default=0.5))
    if score < threshold:
        return result
    await mem.l3.save(user_id, text[:500], score, ["auto_save"])
    result["saved_l3"] = True
    await graph.add_node(user_id, text[:500], "fact", [], score)
    result["saved_graph"] = True
    if score >= 0.8:
        await mem.remember("auto_save", text[:500], score)
        result["saved_l4"] = True
    return result
