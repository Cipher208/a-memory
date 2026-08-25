from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from mcp.server.mcpserver import Context

from mcp_server.models import DreamResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from mcp_server.tools.base import (
    _validate_layer,
    _get_memory,
    _get_rag,
    _fire_hook,
    _truncate_to_budget,
    DEFAULT_TOKEN_BUDGET,
)

# Runtime imports: MCPServer evaluates tool annotations at registration;
# hiding Context/AppContext under TYPE_CHECKING breaks tools/list (fix 419d577).
from mcp.server.mcpserver import Context  # noqa: TC002
from mcp_server.context import AppContext  # noqa: TC001

logger = logging.getLogger(__name__)


async def dream(
    query: str,
    limit: int | None = None,
    layer: Literal["user", "agent"] = "user",
    user_id: str = "default",
    intent: Literal["recent", "core", "balanced"] = "balanced",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: Hybrid search across ALL layers (L3, L4, Wiki, Graph) with context construction."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_dream")

    _validate_layer(layer)

    # 1. Hybrid Search
    multi_rag = _get_rag(app, layer)
    results = await multi_rag.search(query, user_id=user_id, limit=limit, intent=intent)

    # 2. Context Construction
    summary_parts = []
    for r in results:
        title = r.get("title", "Untitled")
        content = r.get("content", "")
        source = r.get("source", "unknown")
        summary_parts.append(f"### {title} (Source: {source})\n{content}")

    full_summary = "\n\n".join(summary_parts)
    summary, truncated = _truncate_to_budget(full_summary, DEFAULT_TOKEN_BUDGET)

    # 3. Hooks
    mem = _get_memory(app, layer, user_id)
    hook_tasks = [
        _fire_hook("auto_context", layer, {"query": query, "results": results}, mem=mem),
        _fire_hook("dream_buffer", layer, {"query": query, "summary": summary, "user_id": user_id}, mem=mem),
    ]
    awaitable_hooks = [t for t in hook_tasks if asyncio.iscoroutine(t)]
    if awaitable_hooks:
        await asyncio.gather(*awaitable_hooks)

    return DreamResult(
        summary=summary,
        truncated=truncated,
        result_count=len(results),
    ).dict()
