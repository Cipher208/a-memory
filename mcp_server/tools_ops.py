from __future__ import annotations

"""Operations tools — auth, backup, saga, data transfer, search.

Merged into action-based tools to reduce tool count.
"""

import asyncio
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING
from mcp.server.fastmcp import Context


from mcp_server.models import (
    ApiKeyResult,
    BackupResult,
    CleanupResult,
    DataResult,
    PurgeResult,
    SearchResult,
)
from mcp_server.registry import _get_ctx, register_tool
from shared.constants import DB_NAME
from shared.metrics import metrics

# if TYPE_CHECKING:

if TYPE_CHECKING:
    pass


async def memory_api_key(
    action: str = "list",
    user_id: str = "default",
    label: str = "",
    api_key: str = "",
    ctx: Context | None = None,
) -> dict:
    """Manage API keys.

    Args:
        action: "create", "revoke", or "list"
        user_id: User to create key for (create only)
        label: Optional label (create only)
        api_key: Key to revoke (revoke only)
    """
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
    """Manage backups.

    Args:
        action: "now", "list", "restore", or "status"
        backup_name: Backup to restore (restore only)
    """
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
    """Run sagas with auto-rollback on failure.

    Args:
        action: "consolidate" (gather->distill->promote) or "backup" (copy->verify)
        user_id: User identifier (consolidate only)
    """
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
    """Import/export memory data.

    Args:
        action: "export", "import", or "list"
        user_id: User to export (export only)
        file_path: File to import (import only)
        target_user_id: Import as this user (import only, defaults to user_id)
    """
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
    """Full memory cleanup: deduplicate, archive, clean staging.

    Args:
        user_id: User identifier
        retention_days: Days to keep audit logs (default 30)
    """
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
    """Emergency purge: delete all data from the last N hours.

    Args:
        user_id: User identifier
        hours: How many hours back to purge (default 24)
    """
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
            conn.close()

    async def _delete_episodes():
        conn = await app.mm.user_memory(user_id).l3._cm.get(DB_NAME)
        try:
            cursor = await conn.execute("DELETE FROM episodes WHERE user_id=? AND created_at > ?", (user_id, cutoff))
            result = cursor.rowcount
            await conn.commit()
            return result
        finally:
            conn.close()

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
            conn.close()

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
            conn.close()

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
    """Hybrid search across RAG + Wiki with strategy selection.

    Args:
        query: Search query
        user_id: User identifier
        limit: Max results (default 10)
        strategy: "fts" (keyword), "mib" (semantic), "hybrid" (combined), or "auto" (auto-select)
        sources: "all" (RAG + Wiki), "rag" (RAG only), or "wiki" (Wiki only)
    """
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


# Backward compatibility alias
memory_search_rrf = memory_search


# Register all ops tools
_register_tools: dict[str, Any] = {
    "memory_api_key": memory_api_key,
    "memory_backup": memory_backup,
    "memory_saga": memory_saga,
    "memory_data": memory_data,
    "memory_sync_replica": memory_sync_replica,
    "memory_cleanup": memory_cleanup,
    "memory_lucidity_purge": memory_lucidity_purge,
    "memory_search": memory_search,
}

for _name, _func in _register_tools.items():
    register_tool(_name, _func)
