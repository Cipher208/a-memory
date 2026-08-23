from __future__ import annotations

from mcp_server.models import SessionResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from .base import _validate_layer, _check_rate_limit, _get_memory, _fire_hook
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import Context



async def memory_session_start(
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Start a new memory session."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_session_start")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return rate_limit

    session_id = await _get_memory(app, layer, user_id).l2.create_session(user_id)
    await _fire_hook("message_received", layer, {"text": "session_started", "session_id": session_id, "user_id": user_id})

    return SessionResult(session_id=session_id).dict()


async def memory_session_end(
    layer: str = "user",
    user_id: str = "default",
    session_id: str = "",
    summary: str = "",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """End a session and save summary."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_session_end")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return rate_limit

    await _get_memory(app, layer, user_id).l2.close_session(session_id, summary)

    await _fire_hook("consolidation", layer, {"trigger": "session_end", "session_id": session_id, "user_id": user_id})
    await _fire_hook("state_delta", layer, {"trigger": "session_end", "session_id": session_id, "summary": summary, "user_id": user_id})

    return SessionResult(status="ok").dict()


async def memory_session_list(
    layer: str = "user",
    user_id: str = "default",
    limit: int = 10,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """List recent memory sessions."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_session_list")
    sessions = await _get_memory(app, layer, user_id).l2.get_recent_sessions(user_id, limit)
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "summary": s.summary,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
            }
            for s in sessions
        ],
        "count": len(sessions),
    }
