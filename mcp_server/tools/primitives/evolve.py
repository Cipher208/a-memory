from __future__ import annotations

import contextlib
import logging
from typing import Any

from mcp.server.mcpserver import Context  # noqa: TC002 — runtime: MCPServer evaluates this annotation at registration

from mcp_server.models import EvolveResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from mcp_server.tools.base import _fire_hook

from mcp_server.context import AppContext  # noqa: TC001 — runtime: MCPServer evaluates this annotation at registration

logger = logging.getLogger(__name__)


async def evolve(
    instruction: str,
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: update agent personality and triggering evolution."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_evolve")

    # Save to Agent's CoreMemory (L4)
    mem = app.mm.agent_memory(user_id)
    await mem.remember("agent_evolution", instruction, importance=1.0)

    # Timeline: personality evolution is a first-class event
    if app.temporal:
        with contextlib.suppress(Exception):
            await app.temporal.add_event(
                user_id,
                "personality_shift",
                instruction[:200],
                importance=1.0,
                layer="agent",
            )

    # Personality shift hook
    hook_result = await _fire_hook("personality_shift", "agent", {"instruction": instruction, "user_id": user_id}, mem=mem)

    summary = hook_result.get("summary", "Personality evolution recorded.")
    return EvolveResult(status="ok", summary=summary).dict()
