from __future__ import annotations
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context
from mcp_server.models import EpisodeResult
from mcp_server.registry import _get_ctx
from shared.constants import DB_NAME
from shared.metrics import metrics

import mcp_server.tools_layer as tl
from .base import _validate_layer, _check_rate_limit, _get_memory, _invalidate_cache

async def memory_episode_save(
    layer: str = "user",
    user_id: str = "default",
    summary: str = "",
    weight: float = 0.5,
    tags: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Save an episode to L3 episodic memory."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_episode_save")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return rate_limit

    episode_id = await _get_memory(app, layer, user_id).l3.save(user_id, summary, weight, tags)
    _invalidate_cache(layer, user_id)

    # Fire post-save hooks
    await tl._fire_hook("emotion_trigger", layer, {"summary": summary, "emotional_weight": weight, "user_id": user_id})
    await tl._fire_hook("state_delta", layer, {"summary": summary, "tags": tags, "user_id": user_id})
    await tl._fire_hook("consolidation", layer, {"trigger": "episode_save", "user_id": user_id})

    return EpisodeResult(episode_id=episode_id).dict()

async def memory_episode_recall(
    layer: str = "user",
    user_id: str = "default",
    tag: str = "",
    limit: int = 10,
    ctx: Context | None = None,
) -> dict:
    """Recall episodes, optionally filtered by tag."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_episode_recall")

    await tl._fire_hook("retrieval_router", layer, {"query": tag or "episodes", "user_id": user_id, "limit": limit})

    mem = _get_memory(app, layer, user_id)
    if tag:
        episodes = await mem.l3.search_by_tag(user_id, tag, limit)
    else:
        episodes = await mem.l3.get_episodes(user_id, limit)
    return EpisodeResult(episodes=[{"id": e.episode_id, "summary": e.summary, "weight": e.emotional_weight} for e in episodes]).dict()

async def memory_episode_list(
    layer: str = "user",
    user_id: str = "default",
    limit: int = 10,
    offset: int = 0,
    ctx: Context | None = None,
) -> dict:
    """List episodes from L3 episodic memory."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_episode_list")
    episodes = await _get_memory(app, layer, user_id).l3.get_episodes(user_id, limit, offset)
    return {
        "episodes": [{"id": e.episode_id, "summary": e.summary, "weight": e.emotional_weight, "tags": e.tags} for e in episodes],
        "count": len(episodes),
    }

async def memory_episode_get(
    layer: str = "user",
    user_id: str = "default",
    episode_id: int = 0,
    ctx: Context | None = None,
) -> dict:
    """Get a single episode by ID."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_episode_get")
    mem = _get_memory(app, layer, user_id)
    conn = await mem.l3._cm.get(DB_NAME)
    cur = await conn.execute(
        "SELECT * FROM episodes WHERE episode_id=? AND user_id=?",
        (episode_id, user_id),
    )
    row = await cur.fetchone()
    if not row:
        return {"error": "episode_not_found", "episode_id": episode_id}
    return {
        "episode_id": row["episode_id"],
        "summary": row["summary"],
        "weight": row["emotional_weight"],
        "tags": mem.l3._row_to_episode(row).tags,
    }
