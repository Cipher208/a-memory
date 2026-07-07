"""Layer tools — unified user/agent memory operations.

All tools accept a `layer` parameter: "user" or "agent".
Rate limiting is applied to all write operations.
Caching is applied to context_inject and recall.
"""

from shared.constants import DB_NAME
import hashlib
import logging
import time
from typing import Any, Optional

from mcp.server.fastmcp import Context

from mcp_server.models import (
    ContextResult,
    EpisodeResult,
    ForgetResult,
    GraphNodeResult,
    RecallResult,
    RememberResult,
    SessionResult,
    StatsResult,
)
from mcp_server.registry import _get_ctx, register_tool
from shared.metrics import metrics

logger = logging.getLogger(__name__)


class _DedupCache:
    _doc_ = "SHA-256 dedup with TTL and periodic cleanup."
    
    def __init__(self, ttl=300, max_size=10000):
        self._cache = {}
        self._ttl = ttl
        self._max_size = max_size
        self._last_cleanup = time.time()
    
    def _cleanup(self):
        now = time.time()
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        expired = [k for k, v in self._cache.items() if now - v > self._ttl]
        for k in expired:
            del self._cache[k]
        if len(self._cache) > self._max_size:
            oldest = sorted(self._cache.keys(), key=lambda k: self._cache[k])[:len(self._cache) // 4]
            for k in oldest:
                del self._cache[k]
    
    def is_duplicate(self, session_id, tool, input_text):
        self._cleanup()
        key = hashlib.sha256(f"{session_id}:{tool}:{input_text[:500]}".encode()).hexdigest()
        now = time.time()
        if key in self._cache and now - self._cache[key] < self._ttl:
            return True
        self._cache[key] = now
        return False


_dedup_cache = _DedupCache(ttl=300, max_size=10000)


def _get_memory(app, layer: str, user_id: str):
    if layer == "agent":
        return app.mm.agent_memory(user_id)
    return app.mm.user_memory(user_id)


def _get_graph(app, layer: str):
    if layer == "agent":
        return app.agent_graph
    return app.user_graph


def _get_wiki(app, layer: str):
    if layer == "agent":
        return app.agent_wiki
    return app.user_wiki


def _get_hooks(app, layer: str):
    if layer == "agent":
        return app.agent_hooks
    return app.user_hooks


_VALID_LAYERS = ("user", "agent")


def _validate_layer(layer: str) -> str:
    """Validate and normalize layer parameter."""
    if layer not in _VALID_LAYERS:
        raise ValueError(f"Invalid layer: {layer!r}. Must be one of {_VALID_LAYERS}")
    return layer


async def _fire_hook(hook_name: str, layer: str, context: dict, mem=None) -> dict:
    """Fire a hook safely — logs errors but never breaks the tool."""
    from hooks.registry import hook_registry

    try:
        return await hook_registry.fire(hook_name, layer, context, mem=mem)
    except Exception as e:
        logger.warning("Hook %s failed: %s", hook_name, e)
        return {"error": str(e)}


async def _check_rate_limit(app, user_id: str) -> dict | None:
    """Check rate limit. Returns error dict if exceeded, None if ok."""
    try:
        result = await app.rate_limiter.check(user_id)
        if not result.get("allowed", True):
            return {
                "error": "rate_limit_exceeded",
                "remaining": result.get("remaining", 0),
                "reset_in": result.get("reset_in", 60),
            }
    except Exception:
        pass
    return None


# Context cache: {key: (timestamp, data)}
_context_cache: dict[str, tuple[float, dict]] = {}
_CONTEXT_CACHE_TTL = 30  # seconds


def _get_cache_key(layer: str, user_id: str) -> str:
    return f"{layer}:{user_id}"


def _get_cached(key: str) -> dict | None:
    if key in _context_cache:
        ts, data = _context_cache[key]
        if time.time() - ts < _CONTEXT_CACHE_TTL:
            return data
    return None


def _set_cached(key: str, data: dict) -> None:
    _context_cache[key] = (time.time(), data)


# Recall cache
_recall_cache: dict[str, tuple[float, list]] = {}
_RECALL_CACHE_TTL = 10  # seconds


def _get_recall_cache(query: str, user_id: str, layer: str, limit: int) -> list | None:
    key = hashlib.md5(f"{layer}:{user_id}:{query}:{limit}".encode()).hexdigest()
    if key in _recall_cache:
        ts, results = _recall_cache[key]
        if time.time() - ts < _RECALL_CACHE_TTL:
            return results
    return None


def _set_recall_cache(query: str, user_id: str, layer: str, limit: int, results: list) -> None:
    key = hashlib.md5(f"{layer}:{user_id}:{query}:{limit}".encode()).hexdigest()
    _recall_cache[key] = (time.time(), results)


async def memory_remember(
    layer: str = "user",
    user_id: str = "default",
    key: str = "",
    value: str = "",
    importance: float = 0.5,
    session_id: str = "",
    ctx: Optional[Context] = None,
) -> dict:
    """Save a fact to long-term memory (L4 CoreMemory).

    Args:
        layer: "user" for user facts, "agent" for agent identity
        user_id: User identifier
        key: Fact key (e.g. "name", "language", "principle")
        value: Fact value
        importance: Importance score 0.0-1.0 (default 0.5)
        session_id: Session ID for dedup (optional)
    """
    # SHA-256 dedup: skip identical calls within 5min window
    if session_id and _dedup_cache.is_duplicate(session_id, key, value):
        logger.info("Dedup: skipping identical remember key=%s user=%s", key, user_id)
        return RememberResult(status="skipped", reason="duplicate_within_ttl").dict()
    
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_remember")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return rate_limit

    hooks = _get_hooks(app, layer)
    gate = await _fire_hook("importance_gate", layer, {"text": value, "key": key, "importance": importance})
    if gate.get("results") and any(r.get("bypass") for r in gate["results"] if isinstance(r, dict)):
        logger.info("Importance gate bypassed: key=%s, importance=%.2f, user=%s", key, importance, user_id)
        return RememberResult(status="skipped", reason="below_importance_threshold").dict()

    mem = _get_memory(app, layer, user_id)

    if layer == "agent":
        import asyncio

        entry_id, node_id = await asyncio.gather(
            mem.remember(key, value, importance),
            _get_graph(app, layer).add_node(
                user_id,
                value,
                "error_analysis" if "error" in key.lower() else "decision_log" if "decision" in key.lower() else "agent_fact",
                ["error_pattern"] if "error" in key.lower() else ["decided_because"] if "decision" in key.lower() else [],
                importance,
            ),
        )
        # Invalidate context cache
        _context_cache.pop(_get_cache_key(layer, user_id), None)

        # Fire post-save hooks
        await _fire_hook("message_received", layer, {"text": value, "key": key, "user_id": user_id}, mem=mem)
        await _fire_hook("emotion_trigger", layer, {"text": value, "user_id": user_id, "key": key}, mem=mem)
        if "error" in key.lower():
            await _fire_hook("error_occurred", layer, {"key": key, "value": value, "user_id": user_id})
        elif "decision" in key.lower():
            await _fire_hook("decision_made", layer, {"key": key, "value": value, "user_id": user_id})
        elif "correction" in key.lower():
            await _fire_hook("self_correction", layer, {"key": key, "value": value, "user_id": user_id})

        return RememberResult(status="ok", entry_id=entry_id, graph_node_id=node_id).dict()

    entry_id = await mem.remember(key, value, importance)

    # Build graph node for user layer (same as agent layer)
    node_id = await _get_graph(app, layer).add_node(
        user_id,
        value,
        "fact",
        [],
        importance,
    )

    # Fire emotion trigger hook (now handles L3 save internally)
    await _fire_hook("emotion_trigger", layer, {"text": value, "user_id": user_id, "key": key}, mem=mem)

    _context_cache.pop(_get_cache_key(layer, user_id), None)

    # Fire post-save hooks
    await _fire_hook("message_received", layer, {"text": value, "key": key, "user_id": user_id}, mem=mem)

    return RememberResult(status="ok", entry_id=entry_id, graph_node_id=node_id).dict()


async def memory_recall(
    layer: str = "user",
    user_id: str = "default",
    query: str = "",
    limit: int = 10,
    ctx: Optional[Context] = None,
) -> dict:
    """Search memory across L3 (episodes) and L4 (facts).

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        query: Search query
        limit: Max results (default 10)
    """
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
    ctx: Optional[Context] = None,
) -> dict:
    """Delete a fact from L4 memory.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        key: Fact key to delete
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_forget")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return rate_limit

    deleted = await _get_memory(app, layer, user_id).forget(key)
    _context_cache.pop(_get_cache_key(layer, user_id), None)
    return ForgetResult(deleted=deleted).dict()


async def memory_session_start(
    layer: str = "user",
    user_id: str = "default",
    ctx: Optional[Context] = None,
) -> dict:
    """Start a new memory session.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
    """
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
    ctx: Optional[Context] = None,
) -> dict:
    """End a session and save summary.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        session_id: Session ID from session_start
        summary: Session summary
    """
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


async def memory_episode_save(
    layer: str = "user",
    user_id: str = "default",
    summary: str = "",
    weight: float = 0.5,
    tags: list[str] | None = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Save an episode to L3 episodic memory.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        summary: Episode description
        weight: Emotional weight 0.0-1.0 (default 0.5)
        tags: Tags (e.g. ["greeting", "decision", "error"])
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_episode_save")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return rate_limit

    episode_id = await _get_memory(app, layer, user_id).l3.save(user_id, summary, weight, tags)
    _context_cache.pop(_get_cache_key(layer, user_id), None)

    # Fire post-save hooks
    await _fire_hook("emotion_trigger", layer, {"summary": summary, "emotional_weight": weight, "user_id": user_id})
    await _fire_hook("state_delta", layer, {"summary": summary, "tags": tags, "user_id": user_id})
    await _fire_hook("consolidation", layer, {"trigger": "episode_save", "user_id": user_id})

    return EpisodeResult(episode_id=episode_id).dict()


async def memory_episode_recall(
    layer: str = "user",
    user_id: str = "default",
    tag: str = "",
    limit: int = 10,
    ctx: Optional[Context] = None,
) -> dict:
    """Recall episodes, optionally filtered by tag.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        tag: Filter by tag (empty = all)
        limit: Max results (default 10)
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_episode_recall")

    await _fire_hook("retrieval_router", layer, {"query": tag or "episodes", "user_id": user_id, "limit": limit})

    mem = _get_memory(app, layer, user_id)
    if tag:
        episodes = await mem.l3.search_by_tag(user_id, tag, limit)
    else:
        episodes = await mem.l3.get_episodes(user_id, limit)
    return EpisodeResult(episodes=[{"id": e.episode_id, "summary": e.summary, "weight": e.emotional_weight} for e in episodes]).dict()


async def memory_graph_add(
    layer: str = "user",
    user_id: str = "default",
    content: str = "",
    node_type: str = "fact",
    tags: list[str] | None = None,
    ctx: Optional[Context] = None,
) -> dict:
    """Add a node to the epistemic graph.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        content: Node content
        node_type: Node type (fact, decision, emotion, error_analysis, etc.)
        tags: Tags
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_graph_add")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return rate_limit

    node_id = await _get_graph(app, layer).add_node(user_id, content, node_type, tags)
    _context_cache.pop(_get_cache_key(layer, user_id), None)

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
    ctx: Optional[Context] = None,
) -> dict:
    """Query the epistemic graph by tag or node type.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        tag: Filter by tag
        node_type: Filter by type
        limit: Max results
    """
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


async def memory_session_list(
    layer: str = "user",
    user_id: str = "default",
    limit: int = 10,
    ctx: Optional[Context] = None,
) -> dict:
    """List recent memory sessions.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        limit: Max results (default 10)
    """
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


async def memory_episode_list(
    layer: str = "user",
    user_id: str = "default",
    limit: int = 10,
    offset: int = 0,
    ctx: Optional[Context] = None,
) -> dict:
    """List episodes from L3 episodic memory.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        limit: Max results (default 10)
        offset: Pagination offset
    """
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
    ctx: Optional[Context] = None,
) -> dict:
    """Get a single episode by ID.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        episode_id: Episode database ID
    """
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


async def memory_graph_nodes(
    layer: str = "user",
    user_id: str = "default",
    node_type: str = "",
    limit: int = 20,
    ctx: Optional[Context] = None,
) -> dict:
    """List nodes from the epistemic graph.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
        node_type: Filter by type (empty = all types)
        limit: Max results (default 20)
    """
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
        nodes = [graph._row_to_node(r) for r in rows]
    return {"nodes": [{"id": n.node_id, "content": n.content, "type": n.node_type, "tags": n.tags} for n in nodes], "count": len(nodes)}


async def memory_graph_edges(
    layer: str = "user",
    user_id: str = "",
    node_id: int = 0,
    limit: int = 20,
    ctx: Optional[Context] = None,
) -> dict:
    """List edges from the epistemic graph.

    Args:
        layer: "user" or "agent"
        node_id: Source node ID (0 = all edges for the layer)
        limit: Max results (default 20)
    """
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


async def memory_stats(
    layer: str = "user",
    user_id: str = "default",
    ctx: Optional[Context] = None,
) -> dict:
    """Get memory statistics for a layer.

    Args:
        layer: "user" or "agent"
        user_id: User identifier
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc("tool_calls")
    metrics.inc("tool_stats")
    mem = _get_memory(app, layer, user_id)
    wiki = _get_wiki(app, layer)
    graph = _get_graph(app, layer)
    l3_count = await mem.l3.count(user_id)
    return StatsResult(
        l1_buffer=mem.l1.size(),
        l2_sessions=await mem.l2.count_sessions(user_id),
        l3_episodes=l3_count,
        l4_facts=await mem.l4.count(user_id),
        wiki_pages=await wiki.count(),
        graph_nodes=await graph.count_nodes(user_id),
    ).dict()


async def memory_context(
    layer: str = "user",
    user_id: str = "default",
    ctx: Optional[Context] = None,
) -> dict:
    """Return compressed context summary for prompt injection (L4 top-10 + L3 top-3 + recent + wiki).

    Args:
        layer: "user" or "agent"
        user_id: User identifier
    """
    metrics.inc("tool_calls")
    metrics.inc("tool_context")

    cache_key = _get_cache_key(layer, user_id)
    cached = _get_cached(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    app = _get_ctx(ctx)
    mem = _get_memory(app, layer, user_id)
    wiki = _get_wiki(app, layer)

    l4_facts = await mem.l4.get_all(user_id, 10)
    facts_text = "; ".join(["%s=%s" % (f.key, f.value[:30]) for f in l4_facts])

    l3_episodes = await mem.l3.get_episodes(user_id, 3)
    episodes_text = "; ".join(["%s" % e.summary[:50] for e in l3_episodes])

    l1_recent = mem.l1.get_recent(5)
    recent_text = "; ".join(["%s: %s" % (r.role, r.content[:50]) for r in l1_recent])

    wiki_entries = await wiki.list_all(3)
    wiki_text = "; ".join(["[%s] %s" % (w.wiki_type, w.title) for w in wiki_entries])

    # Lost-in-the-Middle prevention: L4 at start+end, L2/L1 in middle
    context_parts = []
    if facts_text:
        context_parts.append("CORE FACTS (most important — remember these): " + facts_text)
    if recent_text:
        context_parts.append("RECENT: " + recent_text)
    if wiki_text:
        context_parts.append("WIKI: " + wiki_text)
    if episodes_text:
        context_parts.append("EPISODES: " + episodes_text)
    if facts_text:
        context_parts.append("REMEMBER: " + facts_text)

    result = ContextResult(
        context="\n".join(context_parts),
        l4_facts_count=len(l4_facts),
        l3_episodes_count=len(l3_episodes),
        l1_recent_count=len(l1_recent),
        wiki_count=len(wiki_entries),
    ).dict()
    _set_cached(cache_key, result)
    return result


async def memory_context_inject(
    layer: str = "user",
    user_id: str = "default",
    ctx: Optional[Context] = None,
) -> dict:
    """Return compressed summary for prompt injection (L4 top-10 + L3 top-3).

    Args:
        layer: "user" or "agent"
        user_id: User identifier
    """
    metrics.inc("tool_calls")
    metrics.inc("tool_context_inject")

    await _fire_hook("auto_context", layer, {"query": "context_inject", "user_id": user_id})

    cache_key = _get_cache_key(layer, user_id)
    cached = _get_cached(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    app = _get_ctx(ctx)
    mem = _get_memory(app, layer, user_id)
    wiki = _get_wiki(app, layer)

    # Trigger wiki_agent hook when wiki is accessed
    await _fire_hook("wiki_agent", layer, {"user_id": user_id, "query": "context_inject"})

    l4_facts = await mem.l4.get_all(user_id, 10)
    facts_text = "; ".join(["%s=%s" % (f.key, f.value[:30]) for f in l4_facts])

    l3_episodes = await mem.l3.get_episodes(user_id, 3)
    episodes_text = "; ".join(["%s" % e.summary[:50] for e in l3_episodes])

    l1_recent = mem.l1.get_recent(5)
    recent_text = "; ".join(["%s: %s" % (r.role, r.content[:50]) for r in l1_recent])

    wiki_entries = await wiki.list_all(3)
    wiki_text = "; ".join(["[%s] %s" % (w.wiki_type, w.title) for w in wiki_entries])

    # Lost-in-the-Middle prevention: L4 at start+end, L2/L1 in middle
    # LLMs remember first and last items best, middle is forgotten
    context_parts = []
    if facts_text:
        context_parts.append("CORE FACTS (most important — remember these): " + facts_text)
    if recent_text:
        context_parts.append("RECENT: " + recent_text)
    if wiki_text:
        context_parts.append("WIKI: " + wiki_text)
    if episodes_text:
        context_parts.append("EPISODES: " + episodes_text)
    # Repeat critical facts at the end for recency bias
    if facts_text:
        context_parts.append("REMEMBER: " + facts_text)

    result = {
        "context": "\n".join(context_parts),
        "l4_facts_count": len(l4_facts),
        "l3_episodes_count": len(l3_episodes),
        "l1_recent_count": len(l1_recent),
        "wiki_count": len(wiki_entries),
    }
    _set_cached(cache_key, result)

    # Trigger dream_buffer hook for context staging
    await _fire_hook("dream_buffer", layer, {"text": "\n".join(context_parts), "user_id": user_id})

    return result


# Register all layer tools
_register_tools: dict[str, Any] = {
    "memory_remember": memory_remember,
    "memory_recall": memory_recall,
    "memory_forget": memory_forget,
    "memory_session_start": memory_session_start,
    "memory_session_end": memory_session_end,
    "memory_episode_save": memory_episode_save,
    "memory_episode_recall": memory_episode_recall,
    "memory_graph_add": memory_graph_add,
    "memory_graph_query": memory_graph_query,
    "memory_session_list": memory_session_list,
    "memory_episode_list": memory_episode_list,
    "memory_episode_get": memory_episode_get,
    "memory_graph_nodes": memory_graph_nodes,
    "memory_graph_edges": memory_graph_edges,
    "memory_stats": memory_stats,
    "memory_context": memory_context,
    "memory_context_inject": memory_context_inject,
}

for _name, _func in _register_tools.items():
    register_tool(_name, _func)
