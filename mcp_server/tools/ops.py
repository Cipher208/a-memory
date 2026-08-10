from __future__ import annotations
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context
from mcp_server.models import StatsResult, ContextResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

import mcp_server.tools_layer as tl
from .base import (
    _validate_layer,
    _get_memory,
    _get_wiki,
    _get_graph,
    _get_cached,
    _get_cache_key,
    _set_cached,
    _estimate_tokens,
    _truncate_to_budget,
    DEFAULT_TOKEN_BUDGET,
)

async def memory_stats(
    layer: str = "user",
    user_id: str = "default",
    ctx: Context | None = None,
) -> dict:
    """Get memory statistics for a layer."""
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
    ctx: Context | None = None,
) -> dict:
    """Return compressed context summary for prompt injection."""
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
    facts_text = "; ".join([f"{f.key}={f.value[:30]}" for f in l4_facts])

    l3_episodes = await mem.l3.get_episodes(user_id, 3)
    episodes_text = "; ".join([f"{e.summary[:50]}" for e in l3_episodes])

    l1_recent = mem.l1.get_recent(5)
    recent_text = "; ".join([f"{r.role}: {r.content[:50]}" for r in l1_recent])

    wiki_entries = await wiki.list_all(3)
    wiki_text = "; ".join([f"[{w.wiki_type}] {w.title}" for w in wiki_entries])

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
    ctx: Context | None = None,
) -> dict:
    """Return compressed summary for prompt injection (L4 top-10 + L3 top-3)."""
    metrics.inc("tool_calls")
    metrics.inc("tool_context_inject")

    await tl._fire_hook("auto_context", layer, {"query": "context_inject", "user_id": user_id})

    cache_key = _get_cache_key(layer, user_id)
    cached = _get_cached(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    app = _get_ctx(ctx)
    mem = _get_memory(app, layer, user_id)
    wiki = _get_wiki(app, layer)

    await tl._fire_hook("wiki_agent", layer, {"user_id": user_id, "query": "context_inject"})

    l4_facts = await mem.l4.get_all(user_id, 10)
    facts_text = "; ".join([f"{f.key}={f.value[:30]}" for f in l4_facts])

    l3_episodes = await mem.l3.get_episodes(user_id, 3)
    episodes_text = "; ".join([f"{e.summary[:50]}" for e in l3_episodes])

    l1_recent = mem.l1.get_recent(5)
    recent_text = "; ".join([f"{r.role}: {r.content[:50]}" for r in l1_recent])

    wiki_entries = await wiki.list_all(3)
    wiki_text = "; ".join([f"[{w.wiki_type}] {w.title}" for w in wiki_entries])

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

    context_text = "\n".join(context_parts)
    context_text, was_truncated = _truncate_to_budget(context_text, DEFAULT_TOKEN_BUDGET)

    result = {
        "context": context_text,
        "l4_facts_count": len(l4_facts),
        "l3_episodes_count": len(l3_episodes),
        "l1_recent_count": len(l1_recent),
        "wiki_count": len(wiki_entries),
        "estimated_tokens": _estimate_tokens(context_text),
        "was_truncated": was_truncated,
        "token_budget": DEFAULT_TOKEN_BUDGET,
    }
    _set_cached(cache_key, result)
    await tl._fire_hook("dream_buffer", layer, {"text": context_text, "user_id": user_id})

    return result
