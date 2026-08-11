from __future__ import annotations
import asyncio
import time
from pathlib import Path

from mcp_server.models import (
    StatsResult,
    ContextResult,
    ApiKeyResult,
    BackupResult,
    CleanupResult,
    PurgeResult,
    SearchResult,
    DataResult,
)
from mcp_server.registry import _get_ctx
from shared.metrics import metrics
from shared.constants import DB_NAME

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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context

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

async def memory_api_key(
    action: str = "list",
    user_id: str = "default",
    label: str = "",
    api_key: str = "",
    ctx: Context | None = None,
) -> dict:
    """Manage API keys."""
    from features.auth import api_key_auth

    metrics.inc("tool_calls")
    metrics.inc("tool_api_key")

    if action == "create":
        key = api_key_auth.create_key(user_id, label)
        return ApiKeyResult(api_key=key, user_id=user_id, label=label).dict()
    if action == "revoke":
        revoked = api_key_auth.revoke(api_key)
        return ApiKeyResult(revoked=revoked).dict()
    return ApiKeyResult(keys=api_key_auth.list_keys()).dict()

async def memory_backup(
    action: str = "status",
    backup_name: str = "",
    ctx: Context | None = None,
) -> dict:
    """Manage backups."""
    from features.backup_cron import backup_cron

    metrics.inc("tool_calls")
    metrics.inc("tool_backup")

    if action == "now":
        path = backup_cron.backup_now()
        return BackupResult(path=path).dict()
    if action == "list":
        return BackupResult(backups=backup_cron.list_backups()).dict()
    if action == "restore":
        result = backup_cron.restore(backup_name)
        return BackupResult(**result).dict()
    status = backup_cron.status()
    return BackupResult(**status).dict()

async def memory_saga(
    action: str = "consolidate",
    user_id: str = "default",
    ctx: Context | None = None,
) -> dict:
    """Run sagas with auto-rollback on failure."""
    metrics.inc("tool_calls")
    metrics.inc("tool_saga")
    app = _get_ctx(ctx)

    from shared.saga import SagaEngine, FileSagaStore, SAGA_DIR, create_consolidation_saga, create_backup_saga, SagaState

    store = FileSagaStore(SAGA_DIR)
    engine = SagaEngine(store)

    if action == "consolidate":
        steps = create_consolidation_saga(user_id, mm=app.mm)
        state = SagaState(name=f"consolidation_{user_id}", context={"user_id": user_id, "_mm": app.mm})
    else:
        steps = create_backup_saga()
        state = SagaState(name="backup")

    result = await engine.execute(state, steps)
    return {"status": state.status.value, "result": result, "saga_id": state.saga_id}

async def memory_data(
    action: str = "list",
    user_id: str = "default",
    file_path: str = "",
    target_user_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Import/export memory data."""
    metrics.inc("tool_calls")
    metrics.inc("tool_data")
    app = _get_ctx(ctx)

    if action == "export":
        path = await app.import_export.export_user(user_id)
        return DataResult(path=path).dict()
    if action == "import":
        result = await app.import_export.import_user(file_path, target_user_id or user_id)
        return DataResult(**result).dict()
    return DataResult(exports=app.import_export.list_exports()).dict()

async def memory_sync_replica(
    ctx: Context | None = None,
) -> dict:
    """Sync read-only replica for dashboard/metrics."""
    metrics.inc("tool_calls")
    metrics.inc("tool_sync_replica")
    from shared.read_only import read_only_replica

    result = await asyncio.to_thread(read_only_replica.sync)
    return {"synced": result, "ready": read_only_replica.is_ready()}

async def memory_cleanup(
    user_id: str = "default",
    retention_days: int = 30,
    ctx: Context | None = None,
) -> dict:
    """Full memory cleanup: deduplicate, archive, clean staging."""
    metrics.inc("tool_calls")
    metrics.inc("tool_cleanup")

    from features.audit_trail import AuditTrail
    from features.backup_cron import backup_cron
    from features.compression import MemoryCompressor
    from shared.dream_buffer import DreamBuffer
    from shared.saga import saga_watchdog

    mc = MemoryCompressor()
    at = AuditTrail()
    dream_buf = DreamBuffer()
    archive_dir = str(Path.home() / ".mcp-ariel-memory" / "archives")

    dedup_task = mc.deduplicate_core(user_id)
    compress_task = mc.compress_episodes(user_id, 0.3)
    dream_task = dream_buf.cleanup_old(24, 500)
    audit_task = at.archive_and_prune(retention_days, archive_dir)
    backup_task = asyncio.to_thread(backup_cron._cleanup_old)

    dedup_r, compress_r, dream_r, audit_r, backup_r = await asyncio.gather(dedup_task, compress_task, dream_task, audit_task, backup_task)

    return CleanupResult(
        dedup_core=dedup_r,
        compress_episodes=compress_r,
        dream_buffer_cleanup=dream_r,
        audit_archive=audit_r,
        backup_cleanup=backup_r,
        saga_cleanup=saga_watchdog.cleanup_completed(),
    ).dict()

async def memory_lucidity_purge(
    user_id: str = "default",
    hours: int = 24,
    ctx: Context | None = None,
) -> dict:
    """Emergency purge: delete all data from the last N hours."""
    metrics.inc("tool_calls")
    metrics.inc("tool_lucidity_purge")
    app = _get_ctx(ctx)
    cutoff = time.time() - (hours * 3600)

    async def _delete_core():
        conn = await app.mm.user_memory(user_id).l4._cm.get(DB_NAME)
        try:
            cursor = await conn.execute("DELETE FROM core_memory WHERE user_id=? AND created_at > ?", (user_id, cutoff))
            result = cursor.rowcount
            await conn.commit()
            return result
        finally:
            await conn.close()

    async def _delete_episodes():
        conn = await app.mm.user_memory(user_id).l3._cm.get(DB_NAME)
        try:
            cursor = await conn.execute("DELETE FROM episodes WHERE user_id=? AND created_at > ?", (user_id, cutoff))
            result = cursor.rowcount
            await conn.commit()
            return result
        finally:
            await conn.close()

    async def _delete_staging():
        from shared.dream_buffer import DreamBuffer

        db = DreamBuffer()
        return db.clear_staging(user_id)

    async def _delete_audit():
        from features.audit_trail import AuditTrail

        at = AuditTrail()
        conn = await at._cm.get(DB_NAME)
        try:
            cursor = await conn.execute("DELETE FROM audit_log WHERE user_id=? AND timestamp > ?", (user_id, cutoff))
            result = cursor.rowcount
            await conn.commit()
            return result
        finally:
            await conn.close()

    async def _delete_graph():
        from graph.epistemic import EpistemicGraph

        eg = EpistemicGraph(layer="user")
        conn = await eg._cm.get(DB_NAME)
        try:
            cursor = await conn.execute("DELETE FROM epi_nodes WHERE user_id=? AND created_at > ?", (user_id, cutoff))
            result = cursor.rowcount
            await conn.commit()
            return result
        finally:
            await conn.close()

    core_r, episodes_r, staging_r, audit_r, graph_r = await asyncio.gather(
        _delete_core(), _delete_episodes(), _delete_staging(), _delete_audit(), _delete_graph()
    )

    return PurgeResult(
        core_memory=core_r,
        episodes=episodes_r,
        staging=staging_r,
        audit=audit_r,
        graph_nodes=graph_r,
    ).dict()

async def memory_search(
    query: str = "",
    user_id: str = "default",
    limit: int = 10,
    strategy: str = "hybrid",
    sources: str = "all",
    ctx: Context | None = None,
) -> dict:
    """Hybrid search across RAG + Wiki with strategy selection."""
    metrics.inc("tool_calls")
    metrics.inc("tool_search")
    app = _get_ctx(ctx)

    include_rag = sources in ("all", "rag")
    include_wiki = sources in ("all", "wiki")

    results = await app.user_multi.search(
        query,
        user_id=user_id,
        strategy=strategy,
        limit=limit,
        include_rag=include_rag,
        include_wiki=include_wiki,
    )
    return SearchResult(results=results, count=len(results), method=strategy).dict()

