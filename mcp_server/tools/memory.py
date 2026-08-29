from __future__ import annotations
import logging
import asyncio

from mcp_server.models import RememberResult, RecallResult, ForgetResult
from mcp_server.registry import _get_ctx
from mcp_server.utils.privacy import strip_secrets
from shared.metrics import metrics

from .base import (
    _validate_layer,
    _check_rate_limit,
    _get_memory,
    _get_graph,
    _dedup_cache,
    _invalidate_cache,
    _get_recall_cache,
    _set_recall_cache,
    _fire_hook,
)
from typing import Any

# Runtime import: MCPServer evaluates tool annotations at registration;
# hiding Context under TYPE_CHECKING breaks tools/list (fix 419d577).
from mcp.server.mcpserver import Context  # noqa: TC002


logger = logging.getLogger(__name__)


async def memory_remember(
    layer: str = "user",
    user_id: str = "default",
    key: str = "",
    value: str = "",
    importance: float = 0.5,
    session_id: str = "",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Save a fact to long-term memory (L4 CoreMemory)."""
    value = strip_secrets(value)
    if session_id and _dedup_cache.is_duplicate(session_id, key, value):
        logger.info("Dedup: skipping identical remember key=%s user=%s", key, user_id)
        return RememberResult(status="skipped", reason="duplicate_within_ttl").dict()

    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_remember")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return dict(rate_limit)

    gate = await _fire_hook("importance_gate", layer, {"text": value, "key": key, "importance": importance})
    if gate.get("results") and any(r.get("bypass") for r in gate["results"] if isinstance(r, dict)):
        logger.info("Importance gate bypassed: key=%s, importance=%.2f, user=%s", key, importance, user_id)
        return RememberResult(status="skipped", reason="below_importance_threshold").dict()

    mem = _get_memory(app, layer, user_id)
    graph = _get_graph(app, layer)

    if layer == "agent":
        entry_id, node_id = await asyncio.gather(
            mem.remember(key, value, importance),
            graph.add_node(
                user_id,
                value,
                "error_analysis" if "error" in key.lower() else "decision_log" if "decision" in key.lower() else "agent_fact",
                ["error_pattern"] if "error" in key.lower() else ["decided_because"] if "decision" in key.lower() else [],
                importance,
            ),
        )
        await _fire_post_remember_hooks(layer, user_id, key, value, mem, graph)
    else:
        entry_id = await mem.remember(key, value, importance)
        node_id = await graph.add_node(user_id, value, "fact", [], importance)
        await _fire_hook("emotion_trigger", layer, {"text": value, "user_id": user_id, "key": key}, mem=mem, graph=graph)
        await _fire_hook("message_received", layer, {"text": value, "key": key, "user_id": user_id}, mem=mem, graph=graph)

    _invalidate_cache(layer, user_id)
    return RememberResult(status="ok", entry_id=entry_id, graph_node_id=node_id).dict()


async def _fire_post_remember_hooks(layer: str, user_id: str, key: str, value: str, mem: Any, graph: Any = None) -> None:
    await _fire_hook("message_received", layer, {"text": value, "key": key, "user_id": user_id}, mem=mem, graph=graph)
    # User-emotion analysis on agent texts is semantically wrong; agent
    # self-reflection has its own hooks (error/decision/personality/emotion_context).
    if layer == "user":
        await _fire_hook("emotion_trigger", layer, {"text": value, "user_id": user_id, "key": key}, mem=mem, graph=graph)
    if "error" in key.lower():
        await _fire_hook("error_occurred", layer, {"key": key, "value": value, "user_id": user_id})
    elif "decision" in key.lower():
        await _fire_hook("decision_made", layer, {"key": key, "value": value, "user_id": user_id})
    elif "correction" in key.lower():
        await _fire_hook("self_correction", layer, {"key": key, "value": value, "user_id": user_id})


async def memory_recall(
    layer: str = "user",
    user_id: str = "default",
    query: str = "",
    limit: int = 10,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Search memory across L3 (episodes) and L4 (facts)."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_recall")

    await _fire_hook("retrieval_router", layer, {"query": query, "user_id": user_id, "limit": limit})

    cached = _get_recall_cache(query, user_id, layer, limit)
    if cached is not None:
        return {**RecallResult(results=cached, count=len(cached)).dict(), "cached": True}

    results = await _get_memory(app, layer, user_id).recall(query, limit)
    _set_recall_cache(query, user_id, layer, limit, results)

    await _fire_hook("auto_context", layer, {"query": query, "results_count": len(results), "user_id": user_id})

    return RecallResult(results=results, count=len(results)).dict()


async def memory_forget(
    layer: str = "user",
    user_id: str = "default",
    key: str = "",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Delete a fact from L4 memory."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_forget")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return dict(rate_limit)

    deleted = await _get_memory(app, layer, user_id).forget(key)
    _invalidate_cache(layer, user_id)
    return ForgetResult(deleted_l4=deleted).dict()
