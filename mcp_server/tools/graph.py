from __future__ import annotations

from mcp_server.models import GraphNodeResult
from mcp_server.registry import _get_ctx
from shared.constants import DB_NAME
from shared.metrics import metrics

from .base import _validate_layer, _check_rate_limit, _get_graph, _invalidate_cache, _fire_hook
from typing import Any

# Runtime import: MCPServer evaluates tool annotations at registration;
# hiding Context under TYPE_CHECKING breaks tools/list (fix 419d577).
from mcp.server.mcpserver import Context  # noqa: TC002


async def memory_graph_add(
    layer: str = "user",
    user_id: str = "default",
    content: str = "",
    node_type: str = "fact",
    tags: list[str] | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Add a node to the epistemic graph."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_graph_add")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return dict(rate_limit)

    node_id = await _get_graph(app, layer).add_node(user_id, content, node_type, tags)
    _invalidate_cache(layer, user_id)

    # Fire graph-specific hooks
    hook_map = {
        "error_analysis": "error_occurred",
        "decision_log": "decision_made",
        "personality": "personality_shift",
        "emotion": "emotion_context",
    }
    hook_name = hook_map.get(node_type)
    if hook_name:
        await _fire_hook(hook_name, layer, {"node_type": node_type, "content": content, "user_id": user_id})

    return GraphNodeResult(node_id=node_id).dict()


async def memory_graph_query(
    layer: str = "user",
    user_id: str = "default",
    tag: str = "",
    node_type: str = "",
    limit: int = 20,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Query the epistemic graph by tag or node type."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_graph_query")

    await _fire_hook("retrieval_router", layer, {"query": tag or node_type, "user_id": user_id, "limit": limit})

    graph = _get_graph(app, layer)
    if tag:
        nodes = await graph.query_by_tag(user_id, tag, limit)
    elif node_type:
        nodes = await graph.query_by_type(user_id, node_type, limit)
    else:
        nodes = []
    return GraphNodeResult(nodes=[{"id": n.node_id, "content": n.content, "type": n.node_type, "tags": n.tags} for n in nodes]).dict()


async def memory_graph_nodes(
    layer: str = "user",
    user_id: str = "default",
    node_type: str = "",
    limit: int = 20,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """List nodes from the epistemic graph."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_graph_nodes")
    graph = _get_graph(app, layer)
    if node_type:
        nodes = await graph.query_by_type(user_id, node_type, limit)
    else:
        conn = await graph._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM epi_nodes WHERE layer=? AND user_id=? ORDER BY confidence DESC LIMIT ?",
            (graph.layer, user_id, limit),
        )
        rows = await cur.fetchall()
        nodes = [graph._row_to_node(dict(r)) for r in rows]
    return {"nodes": [{"id": n.node_id, "content": n.content, "type": n.node_type, "tags": n.tags} for n in nodes], "count": len(nodes)}


async def memory_graph_edges(
    layer: str = "user",
    user_id: str = "",
    node_id: int = 0,
    limit: int = 20,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """List edges from the epistemic graph."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_graph_edges")
    graph = _get_graph(app, layer)
    conn = await graph._cm.get(DB_NAME)
    if node_id:
        cur = await conn.execute(
            """SELECT e.source_id, e.target_id, e.relation, e.weight,
                      s.content as src_content, t.content as tgt_content
               FROM epi_edges e
               JOIN epi_nodes s ON e.source_id = s.node_id
               JOIN epi_nodes t ON e.target_id = t.node_id
               WHERE e.source_id = ? AND s.layer = ?
               ORDER BY e.weight DESC LIMIT ?""",
            (node_id, graph.layer, limit),
        )
    else:
        cur = await conn.execute(
            """SELECT e.source_id, e.target_id, e.relation, e.weight,
                      s.content as src_content, t.content as tgt_content
               FROM epi_edges e
               JOIN epi_nodes s ON e.source_id = s.node_id
               JOIN epi_nodes t ON e.target_id = t.node_id
               WHERE s.layer = ?
               ORDER BY e.weight DESC LIMIT ?""",
            (graph.layer, limit),
        )
    rows = await cur.fetchall()
    edges = [
        {
            "source": r[0],
            "target": r[1],
            "relation": r[2],
            "weight": r[3],
            "source_content": r[4],
            "target_content": r[5],
        }
        for r in rows
    ]
    return {"edges": edges, "count": len(edges)}
