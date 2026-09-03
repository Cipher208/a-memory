"""wiki_reflect / wiki_query tools (A1.3 + A1.4) — wiki insight surfaces."""

from __future__ import annotations

from typing import Any, Literal

from mcp_server.registry import _get_ctx
from mcp_server.tools.base import _validate_layer
from shared.metrics import metrics

from mcp_server.tools.base import _get_ctx as _get_app_ctx
from mcp_server.tools.base import _get_wiki


async def wiki_reflect(
    layer: str = "user",
    limit: int = 50,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Wiki outcome digest (A1.3): status/type counts, top pages, staleness signal."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_wiki_reflect")
    from features.wiki_reflect import wiki_reflect as _reflect

    return await _reflect(layer=layer, limit=int(limit))


async def wiki_query(
    layer: str = "user",
    user_id: str = "default",
    path: str = "",
    depth: int = 2,
    link_type: str | None = None,
    direction: Literal["out", "in", "both"] = "out",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """BFS traversal over typed wiki links (A1.4): relationship context for a page."""
    app = _get_app_ctx(ctx)
    layer = _validate_layer(layer)
    _get_wiki(app, layer)  # validate wiki availability for the layer
    metrics.inc("tool_calls")
    metrics.inc("tool_wiki_query")
    from features.wiki_query import wiki_query_bfs

    if not path:
        raise ValueError("path is required (the page to traverse from)")
    return await wiki_query_bfs(
        path,
        depth=int(depth),
        layer=layer,
        link_type=link_type,
        direction=direction,
    )
