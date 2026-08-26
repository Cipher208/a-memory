from __future__ import annotations

import logging
import time
from typing import Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002 — runtime: MCPServer evaluates this annotation at registration

from mcp_server.models import ForgetResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from mcp_server.tools.base import (
    _validate_layer,
    _check_rate_limit,
    _get_memory,
    _get_graph,
    _invalidate_cache,
)

from mcp_server.context import AppContext  # noqa: TC001 — runtime: MCPServer evaluates this annotation at registration

logger = logging.getLogger(__name__)


async def forget(
    key: str,
    scope: Literal["exact", "fuzzy", "recent"] = "exact",
    layer: Literal["user", "agent"] = "user",
    user_id: str = "default",
    minutes: int = 60,
    shadow_bin: bool = True,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: context-aware forgetting with Shadow Bin support."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_forget_primitive")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return dict(rate_limit)

    _validate_layer(layer)
    mem = _get_memory(app, layer, user_id)
    graph = _get_graph(app, layer)

    if scope == "exact":
        deleted_l4, deleted_l3, deleted_graph = await _forget_exact(app, key, user_id, mem, shadow_bin)
    elif scope == "fuzzy":
        deleted_l4, deleted_l3, deleted_graph = await _forget_fuzzy(app, key, user_id, mem, graph, shadow_bin)
    elif scope == "recent":
        deleted_l4, deleted_l3, deleted_graph = await _forget_recent(app, user_id, minutes, mem, graph)
    else:
        deleted_l4 = deleted_l3 = deleted_graph = 0

    _invalidate_cache(layer, user_id)
    return ForgetResult(deleted_l4=deleted_l4, deleted_l3=deleted_l3, deleted_graph=deleted_graph).dict()


async def _forget_exact(app: AppContext, key: str, user_id: str, mem: Any, shadow_bin: bool) -> tuple[int, int, int]:
    """Delete one L4 key, archiving it first when Shadow Bin is on."""
    am = await _archived_memories(app)
    entry = await mem.l4.get(user_id, key)
    if not entry:
        return 0, 0, 0

    if shadow_bin:
        await am.archive(
            user_id=user_id,
            content=f"{entry.key}={entry.value}",
            memory_type=entry.memory_kind,
            importance=entry.importance,
            original_id=entry.entry_id,
            reason="forget_primitive_exact",
        )
    await mem.forget(key)
    return 1, 0, 0


async def _forget_fuzzy(app: AppContext, key: str, user_id: str, mem: Any, graph: Any, shadow_bin: bool) -> tuple[int, int, int]:
    """Pattern delete across L4, L3 and Graph, archiving each hit first."""
    from shared.archived_memories import ArchivedMemories

    am = ArchivedMemories(cm=app.mm._cm)
    await am._init_db()

    deleted_l4 = 0
    l4_hits = await mem.l4.search(user_id, key, limit=10)
    for hit in l4_hits:
        entry = await mem.l4.get(user_id, hit["key"])
        if not entry:
            continue
        if shadow_bin:
            await am.archive(
                user_id=user_id,
                content=f"{entry.key}={entry.value}",
                memory_type=entry.memory_kind,
                importance=entry.importance,
                original_id=entry.entry_id,
                reason="forget_primitive_fuzzy_l4",
            )
        if await mem.l4.delete(user_id, hit["key"]):
            deleted_l4 += 1

    deleted_l3 = await _delete_matching_episodes(am, key, user_id, mem, shadow_bin)
    deleted_graph = await _delete_matching_nodes(am, key, user_id, graph, shadow_bin)
    return deleted_l4, deleted_l3, deleted_graph


async def _delete_matching_episodes(am: Any, key: str, user_id: str, mem: Any, shadow_bin: bool) -> int:
    episodes = await mem.l3.search(user_id, key, limit=10)
    if shadow_bin:
        for e in episodes:
            await am.archive(
                user_id=user_id,
                content=e.summary,
                memory_type="episode",
                importance=e.emotional_weight,
                original_id=e.episode_id,
                reason="forget_primitive_fuzzy_l3",
            )
    return int(await mem.l3.delete_by_ids([e.episode_id for e in episodes]))


async def _delete_matching_nodes(am: Any, key: str, user_id: str, graph: Any, shadow_bin: bool) -> int:
    nodes = await graph.find_nodes_matching(user_id, f"%{key}%")
    if shadow_bin:
        for n in nodes:
            await am.archive(
                user_id=user_id,
                content=n.content,
                memory_type=f"graph:{n.node_type}",
                importance=n.confidence,
                original_id=n.node_id,
                reason="forget_primitive_fuzzy_graph",
            )
    return int(await graph.delete_nodes([n.node_id for n in nodes]))


async def _forget_recent(app: AppContext, user_id: str, minutes: int, mem: Any, graph: Any) -> tuple[int, int, int]:
    """Mass purge of everything newer than the window (no shadow bin)."""
    from mcp_server.tools.ops import _purge_staging

    cutoff = time.time() - (minutes * 60)
    deleted_l4 = await mem.l4.delete_older_than(user_id, cutoff)
    deleted_l3 = await mem.l3.delete_older_than(user_id, cutoff)
    deleted_graph = await graph.delete_nodes_older_than(user_id, cutoff)
    await _purge_staging(user_id)
    return deleted_l4, deleted_l3, deleted_graph


async def _archived_memories(app: AppContext) -> Any:
    from shared.archived_memories import ArchivedMemories

    am = ArchivedMemories(cm=app.mm._cm)
    await am._init_db()
    return am
