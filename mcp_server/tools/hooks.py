"""memory_hook — MCP transport for harness-fired lifecycle events (spec S6).

Transport parity with POST /api/hooks/{event}: both call dispatch_event.
This is a harness transport, not agent-initiated memory access — the push
model is unchanged. Registered through _scope_tool (D1.13 user_id binding).
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context  # noqa: TC002 — annotation eval at registration requires runtime import
from mcp_server.registry import _get_ctx

from mcp_server.tools.base import _validate_layer


async def memory_hook(
    event: str,
    payload: dict[str, Any] | None = None,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Fire one external lifecycle event (session_started, new_message, ...).

    Args:
        event: one of KNOWN_EVENTS (session_started, session_ended, new_message,
            auto_save_candidate, context_threshold, memory_pressure,
            post_context_compression).
        payload: event body. Recognized keys per event: session_ended:
            {"summary": str}; new_message/auto_save_candidate: {"text": str};
            post_context_compression: {"query": str}; session_started:
            {"text": str?, "budget": int?}.
        layer: "user" (default) or "agent".
        user_id: subject user (bound to API key on HTTP transports).
        ctx: MCP context (injected).

    Returns:
        Handler results dict; unknown event raises ValueError (MCP error).

    """
    from hooks.external import KNOWN_EVENTS, dispatch_event
    from mcp_server.tools.base import _get_graph, _get_memory, _get_rag

    if event not in KNOWN_EVENTS:
        raise ValueError(f"unknown event: {event!r}. Must be one of {sorted(KNOWN_EVENTS)}")
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    mem = _get_memory(app, layer, user_id)
    graph = _get_graph(app, layer)
    try:
        rag = _get_rag(app, layer)
    except Exception:
        rag = None
    return await dispatch_event(event, layer, user_id, dict(payload or {}), mem, graph, rag)
