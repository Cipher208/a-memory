from __future__ import annotations
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)

# Runtime import: MCPServer evaluates tool annotations at registration;
# hiding Context under TYPE_CHECKING breaks tools/list (fix 419d577).
from mcp.server.mcpserver import Context  # noqa: TC002

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
from shared.constants import DB_NAME, DEFAULT_USER, DEFAULT_LAYER, METRIC_TOOL_CALLS

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
    _fire_hook,
    _invalidate_cache,
    DEFAULT_TOKEN_BUDGET,
)


async def memory_stats(
    layer: str = DEFAULT_LAYER,
    user_id: str = DEFAULT_USER,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Get memory statistics for a layer."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    metrics.inc(METRIC_TOOL_CALLS)
    metrics.inc("tool_stats")
    mem = _get_memory(app, layer, user_id)
    wiki = _get_wiki(app, layer)
    graph = _get_graph(app, layer)
    l3_count = await mem.l3.count(user_id)
    from features.recall_telemetry import count_recalls

    return StatsResult(
        l1_buffer=mem.l1.size(),
        l2_sessions=await mem.l2.count_sessions(user_id),
        l3_episodes=l3_count,
        l4_facts=await mem.l4.count(user_id),
        wiki_pages=await wiki.count(),
        graph_nodes=await graph.count_nodes(user_id),
        avg_session_quality=await mem.l2.avg_quality(user_id),
        recall_count=await count_recalls(app.mm._cm, user_id),
    ).dict()


async def memory_context(
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
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

    # Extra read for the CONTEXT.md snapshot (5 vs 3 — snapshot wants more).
    # memory_context itself never writes the file, so the read is local-only.
    try:
        l3_for_snapshot = await mem.l3.get_episodes(user_id, 5)
    except Exception as exc:
        logger.warning("memory_context_inject: extra l3 read failed: %s", exc)
        l3_for_snapshot = l3_episodes  # noqa: F841  (except-branch rebinding, never read here)

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
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Return compressed summary for prompt injection (L4 top-10 + L3 top-3)."""
    metrics.inc("tool_calls")
    metrics.inc("tool_context_inject")

    await _fire_hook("auto_context", layer, {"query": "context_inject", "user_id": user_id})

    cache_key = _get_cache_key(layer, user_id)
    cached = _get_cached(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    app = _get_ctx(ctx)

    # Inline consolidation step (the "inject" stage of the dream cycle pipeline):
    # drain high-weight L3 episodes into L4 right before we read L4, so the
    # context the agent sees is freshly curated. Failures are non-fatal.
    consolidated = 0
    last_consolidation_ts = time.time()
    try:
        from config import config as _cfg
        from lifecycle.consolidation import ConsolidationEngine

        min_weight = float(_cfg.get_forgetting("consolidate_weight_threshold") or 0.7)
        consolidated = await ConsolidationEngine(layer=layer).consolidate_episodes(
            user_id=user_id,
            min_weight=min_weight,
        )
        _invalidate_cache(layer, user_id)
    except Exception as exc:
        logger.warning("memory_context_inject: consolidation failed: %s", exc)

    mem = _get_memory(app, layer, user_id)
    wiki = _get_wiki(app, layer)

    await _fire_hook("wiki_agent", layer, {"user_id": user_id, "query": "context_inject"})

    l4_facts = await mem.l4.get_all(user_id, 10)
    facts_text = "; ".join([f"{f.key}={f.value[:30]}" for f in l4_facts])

    l3_episodes = await mem.l3.get_episodes(user_id, 3)
    episodes_text = "; ".join([f"{e.summary[:50]}" for e in l3_episodes])

    # Extra read for the CONTEXT.md snapshot (5 vs 3 — snapshot wants more)
    try:
        l3_for_snapshot = await mem.l3.get_episodes(user_id, 5)
    except Exception as exc:
        logger.warning("memory_context_inject: extra l3 read failed: %s", exc)
        l3_for_snapshot = l3_episodes

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

    # CONTEXT.md persistence: 3-section snapshot written per-agent to
    # <MCP_MEMORY_DATA_DIR>/<layer>/CONTEXT.md. Failures are non-fatal.
    context_md_path: str | None = None
    try:
        # _validate_layer already narrowed layer at runtime; cast for mypy.
        layer_lit = cast("Literal['user', 'agent']", layer)
        body = await _build_context_md(
            layer=layer_lit,
            user_id=user_id,
            context_text=context_text,
            consolidated=consolidated,
            last_consolidation_ts=last_consolidation_ts,
            l4_count=len(l4_facts),
            l3_count=len(l3_episodes),
            l1_count=len(l1_recent),
            wiki_count=len(wiki_entries),
            episodes=l3_for_snapshot,
        )
        path = _context_md_path(layer)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, body, encoding="utf-8")
        context_md_path = str(path)
    except Exception as exc:
        logger.warning("memory_context_inject: CONTEXT.md write failed: %s", exc)

    result = {
        "context": context_text,
        "l4_facts_count": len(l4_facts),
        "l3_episodes_count": len(l3_episodes),
        "l1_recent_count": len(l1_recent),
        "wiki_count": len(wiki_entries),
        "estimated_tokens": _estimate_tokens(context_text),
        "was_truncated": was_truncated,
        "token_budget": DEFAULT_TOKEN_BUDGET,
        "consolidated_episodes": consolidated,
        "last_consolidation_ts": last_consolidation_ts,
        "context_md_path": context_md_path,
        "perspectives_count": 6,  # 6 perspectives always written
    }
    _set_cached(cache_key, result)
    await _fire_hook("dream_buffer", layer, {"text": context_text, "user_id": user_id})

    return result


async def memory_api_key(
    action: str = "list",
    user_id: str = "default",
    label: str = "",
    api_key: str = "",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
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
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
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
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
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
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Import/export memory data."""
    metrics.inc("tool_calls")
    metrics.inc("tool_data")
    app = _get_ctx(ctx)

    if action == "export":
        path = await app.import_export.export_user(user_id)
        return DataResult(path=path).dict()
    if action == "import":
        result = await app.import_export.import_user(file_path, target_user_id or user_id)
        for layer in ("user", "agent"):
            _invalidate_cache(layer, target_user_id or user_id)
        return DataResult(**result).dict()
    return DataResult(exports=app.import_export.list_exports()).dict()


async def memory_sync_replica(
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Sync read-only replica for dashboard/metrics."""
    metrics.inc("tool_calls")
    metrics.inc("tool_sync_replica")
    from shared.read_only import read_only_replica

    result = await asyncio.to_thread(read_only_replica.sync)
    return {"synced": result, "ready": read_only_replica.is_ready()}


async def memory_cleanup(
    user_id: str = "default",
    retention_days: int = 30,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Full memory cleanup: deduplicate, archive, clean staging."""
    metrics.inc("tool_calls")
    metrics.inc("tool_cleanup")

    from features.audit_trail import AuditTrail
    from features.backup_cron import backup_cron
    from features.compression import MemoryCompressor
    from shared.dream_buffer import DreamBuffer
    from shared.saga import saga_watchdog
    from lifecycle.forgetting import forgetting_system

    mc = MemoryCompressor()
    at = AuditTrail()
    dream_buf = DreamBuffer()
    archive_dir = str(Path.home() / ".mcp-ariel-memory" / "archives")

    # Parallel dispatch
    results = await asyncio.gather(
        mc.deduplicate_core(user_id),
        mc.compress_episodes(user_id, 0.3),
        dream_buf.cleanup_old(24, 500),
        at.archive_and_prune(retention_days, archive_dir),
        asyncio.to_thread(backup_cron._cleanup_old),
        forgetting_system.run_cleanup(user_id),
    )

    for layer in ("user", "agent"):
        _invalidate_cache(layer, user_id)
    return CleanupResult(
        dedup_core=results[0],
        compress_episodes=results[1],
        dream_buffer_cleanup=results[2],
        audit_archive=results[3],
        backup_cleanup=results[4],
        saga_cleanup=saga_watchdog.cleanup_completed(),
        compaction=results[5].get("archived", 0) if isinstance(results[5], dict) else 0,
    ).dict()


async def memory_lucidity_purge(
    user_id: str = DEFAULT_USER,
    hours: int = 24,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Emergency purge: delete all data from the last N hours."""
    metrics.inc(METRIC_TOOL_CALLS)
    metrics.inc("tool_lucidity_purge")
    app = _get_ctx(ctx)
    cutoff = time.time() - (hours * 3600)

    # Parallel execution using consolidated purge helper
    results = await asyncio.gather(
        _purge_table(app.mm.user_memory(user_id).l4._cm, "core_memory", user_id, cutoff),
        _purge_table(app.mm.user_memory(user_id).l3._cm, "episodes", user_id, cutoff),
        _purge_table(getattr(app.mm, "audit_trail", None), "audit_log", user_id, cutoff, timestamp_col="timestamp"),
        _purge_table(getattr(app.mm, "epistemic_graph", None), "epi_nodes", user_id, cutoff),
        _purge_staging(user_id),
    )

    for layer in ("user", "agent"):
        _invalidate_cache(layer, user_id)
    return PurgeResult(
        core_memory=results[0],
        episodes=results[1],
        audit=results[2],
        graph_nodes=results[3],
        staging=results[4],
    ).dict()


async def _purge_table(cm: Any, table: str, user_id: str, cutoff: float, timestamp_col: str = "created_at") -> int:
    """Consolidated table purge logic to eliminate code clones."""
    if not cm:
        return 0
    conn = await cm.get(DB_NAME)
    try:
        # Table names are static in this context, validated by internal callers.
        sql = f"DELETE FROM {table} WHERE user_id=? AND {timestamp_col} > ?"
        cursor = await conn.execute(sql, (user_id, cutoff))
        result = int(cursor.rowcount)
        await conn.commit()
        return result
    except Exception:
        return 0


async def _purge_staging(user_id: str) -> int:
    from shared.dream_buffer import DreamBuffer

    db = DreamBuffer()
    return await db.clear_staging(user_id)


async def memory_search(
    query: str = "",
    user_id: str = "default",
    limit: int = 10,
    strategy: str = "hybrid",
    sources: str = "all",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
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


# ── CONTEXT.md persistence (research backlog 3.13) ──────────────


def _context_md_path(layer: str) -> Path:
    """Resolve <MCP_MEMORY_DATA_DIR>/<layer>/CONTEXT.md.

    Per-agent isolation: each agent has its own MCP_MEMORY_DATA_DIR
    (hermes/mimocode/cowagent configs set it differently), so the file
    is naturally per-agent and never collides.
    """
    raw = os.environ.get("MCP_MEMORY_DATA_DIR", "~/.mcp-ariel-memory")
    data_dir = Path(raw).expanduser()
    return data_dir / layer / "CONTEXT.md"


async def _build_context_md(
    *,
    layer: Literal["user", "agent"],
    user_id: str,
    context_text: str,
    consolidated: int,
    last_consolidation_ts: float,
    l4_count: int,
    l3_count: int,
    l1_count: int,
    wiki_count: int,
    episodes: list[Any],  # WikiEntry-like (have .summary, .emotional_weight)
) -> str:
    """Compose the CONTEXT.md content: frontmatter + 3 sections.

    Returns the full file content as a string. Caller writes to disk.
    """
    from mcp_server.tools.wiki_summarize import wiki_summarize

    perspective_names = [
        "practical",
        "epistemic",
        "psychological",
        "social",
        "temporal",
        "metacognitive",
    ]
    perspective_blocks: list[str] = []
    for p in perspective_names:
        try:
            res = await wiki_summarize(perspective=p, layer=layer, limit=3)
            digest = res.get("digest", "")
            digest, _ = _truncate_to_budget(digest, 200)
            title = p.capitalize()
            wt = res.get("wiki_type", "?")
            block = f"### {title} ({wt})\n\n{digest}\n"
        except Exception as exc:
            logger.warning("CONTEXT.md: wiki_summarize %s failed: %s", p, exc)
            block = f"### {p.capitalize()}\n\n_(unavailable)_\n"
        perspective_blocks.append(block)

    episode_lines: list[str] = []
    for e in episodes[:5]:
        weight = float(getattr(e, "emotional_weight", 0.0))
        summary = (getattr(e, "summary", "") or "")[:120]
        episode_lines.append(f"- [weight={weight:.1f}] {summary}")
    if not episode_lines:
        episode_lines.append("_(no recent episodes)_")

    fm_lines = [
        "---",
        f"generated_at: {time.time()}",
        f"layer: {layer}",
        f"user_id: {user_id}",
        f"token_budget: {DEFAULT_TOKEN_BUDGET}",
        f"consolidated_episodes: {consolidated}",
        f"last_consolidation_ts: {last_consolidation_ts}",
        "sources:",
        f"  l4: {l4_count}",
        f"  l3: {l3_count}",
        f"  l1: {l1_count}",
        f"  wiki: {wiki_count}",
        "schema_version: 1",
        "---",
    ]

    return (
        "\n".join(fm_lines)
        + "\n\n"
        + f"# Context Snapshot — {layer} layer\n\n"
        + "## Context\n\n"
        + context_text
        + "\n\n## Perspectives\n\n"
        + "\n".join(perspective_blocks)
        + "\n## Recent Episodes\n\n"
        + "\n".join(episode_lines)
        + "\n\n---\n*Auto-generated by a-memory memory_context_inject.*\n"
    )


# ─── memory_watch (Phase C C1.10 S6) ────────────────────────────────────────────
# Operator introspection of the rules ariel's auto_save_text applies. The rules
# do not introduce new behavior; they describe what ariel already does. See
# hooks/external.py::auto_save_text for the actual save logic.

import json as _json
import sqlite3 as _sqlite3
import time as _time

from shared.connection import connection_manager

_ALLOWED_PREDICATE_KEYS = frozenset({"min_importance", "l4_min_importance", "keywords", "sender"})


def _validate_predicate(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - _ALLOWED_PREDICATE_KEYS
    if unknown:
        raise ValueError(f"predicate key not allowed: {sorted(unknown)}")
    for k in ("min_importance", "l4_min_importance"):
        if k in raw:
            v = float(raw[k])
            if not 0 <= v <= 1:
                raise ValueError(f"predicate.{k} must be in [0, 1]")
    return raw


async def memory_watch(
    action: str,
    *,
    name: str = "",
    trigger: str = "",
    predicate_json: str = "",
    action_kind: str = "",
    rule_id: int = 0,
    enabled_only: bool = False,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Operator introspection of the rules ariel's auto_save_text applies (C1.10 S6).

    The rules do not introduce new behavior; they describe what ariel already
    does. `add` and `disable` change operator-visible state only; the live
    dispatch path runs on the same threshold bands regardless.

    action: "list" | "add" | "disable" | "delete"
    """
    _ = _get_ctx(ctx)
    db_path = connection_manager.base_dir / DB_NAME
    if not db_path.exists():
        raise RuntimeError("watch_rules table not initialized; run alembic upgrade head")

    if action == "list":
        sql = "SELECT id, name, trigger, predicate, action, enabled, created_at FROM watch_rules"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with _sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(sql, params).fetchall()
        rules: list[dict[str, Any]] = []
        for r in rows:
            rid, rname, rtrig, rpred, ract, renabled, _rcreated = r
            try:
                pred_obj = _json.loads(rpred)
            except Exception:
                pred_obj = {"raw": rpred}
            cutoff = _time.time() - 86400
            with _sqlite3.connect(str(db_path)) as conn:
                hit = conn.execute(
                    "SELECT count(*) FROM memory_dispatch_log WHERE event = ? AND created_at >= ?",
                    (rtrig, cutoff),
                ).fetchone()
            rules.append(
                {
                    "id": int(rid),
                    "name": rname,
                    "trigger": rtrig,
                    "predicate": pred_obj,
                    "action": ract,
                    "enabled": bool(renabled),
                    "hits_24h": int(hit[0]) if hit else 0,
                }
            )
        return {"status": "ok", "rules": rules}

    if action == "add":
        if not name or not trigger or not predicate_json or not action_kind:
            raise ValueError("add requires name, trigger, predicate_json, action_kind")
        try:
            pred_obj = _json.loads(predicate_json)
        except Exception as e:
            raise ValueError(f"predicate_json is not valid JSON: {e}") from e
        if not isinstance(pred_obj, dict):
            raise ValueError("predicate must be a JSON object")
        _validate_predicate(pred_obj)
        with _sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO watch_rules (name, trigger, predicate, action, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (name, trigger, _json.dumps(pred_obj, ensure_ascii=False), action_kind, _time.time()),
            )
            conn.commit()
            return {"status": "ok", "id": int(cur.lastrowid or 0)}

    if action == "disable":
        if not rule_id:
            raise ValueError("disable requires rule_id")
        with _sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE watch_rules SET enabled = 0 WHERE id = ?", (rule_id,))
            conn.commit()
        return {"status": "ok"}

    if action == "delete":
        if not rule_id:
            raise ValueError("delete requires rule_id")
        with _sqlite3.connect(str(db_path)) as conn:
            conn.execute("DELETE FROM watch_rules WHERE id = ?", (rule_id,))
            conn.commit()
        return {"status": "ok"}

    raise ValueError(f"unknown action: {action!r}")


# ─── memory_proposals (Phase C C1.11 S5) ───────────────────────────────────────


async def memory_proposals(
    action: str,
    *,
    proposal_id: int = 0,
    approve: bool = True,
    status: str = "pending",
    limit: int = 20,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Review surface for staged mutations (C1.11 S5).

    action: "list" (pending proposals for user_id) | "decide" (apply/reject one
    proposal via proposal_id + approve). The apply path executes the exact write
    the direct path would have done; every decision is audit-logged.
    """
    app = _get_ctx(ctx)  # live AppContext — passed into decide for core_write apply
    if action == "list":
        from features.staging import list_pending

        return {"status": "ok", "proposals": await list_pending(user_id, limit)}
    if action == "decide":
        from features.staging import decide

        result = await decide(proposal_id, approve, mem=app)
        return {"status": "ok", **result}
    if action == "revert":
        from features.staging import revert

        result = await revert(proposal_id, mem=app)
        return {"status": "ok", **result}
    raise ValueError(f"unknown action: {action!r}")


# ─── memory_report_card (Phase C C1.14 S5) ─────────────────────────────────────


async def memory_report_card(
    period_hours: int = 24,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Operator digest: what automation did to memory in the window (C1.14 S5)."""
    if ctx is not None:
        _get_ctx(ctx)  # strict when called over MCP; CLI/one-liners pass ctx=None
    import sqlite3 as _sqlite3
    import time as _time

    from shared.connection import connection_manager

    since = _time.time() - max(1, int(period_hours)) * 3600
    db_path = connection_manager.base_dir / DB_NAME
    card: dict[str, Any] = {"status": "ok", "period_hours": period_hours}
    if not db_path.exists():
        card.update(
            {
                "proposals": {"created": 0, "recent": []},
                "auto_save": {"dispatched": 0, "saved_l3": 0, "saved_l4": 0, "saved_graph": 0},
                "gaps": {"count": 0, "previews": []},
                "dream_markers": 0,
            }
        )
        return card
    with _sqlite3.connect(str(db_path)) as conn:
        prow = conn.execute(
            "SELECT count(*),"
            " sum(CASE WHEN status='applied' THEN 1 ELSE 0 END),"
            " sum(CASE WHEN status='reverted' THEN 1 ELSE 0 END),"
            " sum(CASE WHEN status='rejected' THEN 1 ELSE 0 END),"
            " sum(CASE WHEN status='expired' THEN 1 ELSE 0 END),"
            " sum(CASE WHEN status='pending' THEN 1 ELSE 0 END)"
            " FROM mutation_proposals WHERE proposed_at >= ?",
            (since,),
        ).fetchone()
        recent = conn.execute(
            "SELECT id, kind, status, decided_at FROM mutation_proposals WHERE decided_at IS NOT NULL AND decided_at >= ?"
            " ORDER BY decided_at DESC LIMIT 5",
            (since,),
        ).fetchall()
        drow = conn.execute(
            "SELECT count(*), sum(saved_l3), sum(saved_l4), sum(saved_graph) FROM memory_dispatch_log"
            " WHERE event IN ('new_message', 'auto_save_candidate') AND created_at >= ?",
            (since,),
        ).fetchone()
        dreams = conn.execute(
            "SELECT count(*) FROM mutation_proposals WHERE source = 'dream' AND proposed_at >= ?",
            (since,),
        ).fetchone()

    card["proposals"] = {
        "created": int(prow[0] or 0),
        "applied": int(prow[1] or 0),
        "reverted": int(prow[2] or 0),
        "rejected": int(prow[3] or 0),
        "expired": int(prow[4] or 0),
        "pending": int(prow[5] or 0),
        "recent": [{"id": r[0], "kind": r[1], "status": r[2], "decided_at": r[3]} for r in recent],
    }
    card["auto_save"] = {
        "dispatched": int(drow[0] or 0),
        "saved_l3": int(drow[1] or 0),
        "saved_l4": int(drow[2] or 0),
        "saved_graph": int(drow[3] or 0),
    }
    card["dream_markers"] = int(dreams[0] or 0)
    try:
        from features.diff import compute_session_gaps
        from mcp_server.context import AppContext

        app = AppContext()
        mem = app.mm.user_memory(user_id) if layer == "user" else app.mm.agent_memory(user_id)
        gaps = compute_session_gaps(mem, since, _time.time())
        card["gaps"] = {"count": len(gaps), "previews": [g["text_preview"][:80] for g in gaps[:5]]}
    except Exception:
        card["gaps"] = {"count": 0, "previews": []}
    return card


async def memory_recall_protocol(
    query: str = "",
    budget: int = 2000,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Multi-axis /recall protocol (D1.1): markers → session → semantic → expand → day.

    Proportional: empty query = zero-state (markers + day only). The same
    engine drives the Hermes prefetch surface via `autohooks recall`.
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    from .base import _get_memory, _get_rag

    mem = _get_memory(app, layer, user_id)
    rag = _get_rag(app, layer)
    from features.recall import recall_protocol

    blocks = await recall_protocol(mem, rag, user_id, query=query, budget=int(budget))
    metrics.inc(METRIC_TOOL_CALLS)
    return {"blocks": blocks, "axes": [b["axis"] for b in blocks], "count": len(blocks)}


async def memory_get_smart_context(
    query: str = "",
    budget: int = 2000,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Weighted token distribution across memory sources (D1.10).

    Every source (important/relevant/recent/day/ops) gets a weight-proportional
    floor first; leftover redistributes with a 2x-floor ceiling — a fat source
    can no longer starve the rest (unlike the sequential inject builder).
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    from .base import _get_memory, _get_rag

    mem = _get_memory(app, layer, user_id)
    rag = _get_rag(app, layer)
    from features.smart_context import build_smart_context

    out = await build_smart_context(mem, rag, user_id, query=query, budget=int(budget))
    metrics.inc(METRIC_TOOL_CALLS)
    return out


async def memory_reflect(
    period_hours: int = 24,
    topic: str = "",
    action: Literal["write", "list"] = "write",
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Reflection system (D1.16): deterministic meta-memories.

    `write` computes a reflection over the window (episode counts, recurring
    topics) and stores it as a meta-memory row; `list` reads recent
    reflections back (topic filter optional). The nightly hook's 5th phase
    writes the daily reflection automatically.
    """
    if ctx is not None:
        _get_ctx(ctx)  # strict over MCP; CLI one-liners pass ctx=None
    _ = _validate_layer(layer)
    from features.reflection import build_reflection, save_reflection, list_reflections

    if action == "list":
        rows = list_reflections(user_id, topic=topic, limit=10)
        return {"action": "list", "count": len(rows), "reflections": rows}

    if ctx is not None:
        app = _get_ctx(ctx)
        mem = app.mm.user_memory(user_id) if layer == "user" else app.mm.agent_memory(user_id)
    else:
        from core.episodic import EpisodicMemory

        mem = EpisodicMemory(layer=layer)
    out = build_reflection(mem, user_id, period_hours=int(period_hours))
    rid = save_reflection(user_id, topic=topic or "manual", insight=out["insight"], stats=out["stats"])
    metrics.inc(METRIC_TOOL_CALLS)
    return {"action": "write", "id": rid, "insight": out["insight"], "stats": out["stats"]}


async def memory_scratchpad(
    action: Literal["write", "read", "clear", "promote"] = "read",
    key: str = "",
    content: str = "",
    to: str = "l3",
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Agent scratchpad (D1.15, L2.5): working memory between session and episodes.

    Write hypotheses/plans/drafts (capped at 20 entries, oldest evicted);
    they re-inject at session start as the `scratchpad` block. `promote`
    moves agent-judged-useful entries into L3 (episode) or L4 (fact) and
    drops them from the pad — the agent is the distiller (D2.2 ceiling).
    """
    if ctx is not None:
        _get_ctx(ctx)
    _ = _validate_layer(layer)
    from features.scratchpad import write_entry, read_entries, clear_entries, promote_entries

    metrics.inc(METRIC_TOOL_CALLS)
    if action == "write":
        if not key or not content:
            raise ValueError("write requires key and content")
        ok = await write_entry(user_id, layer, key, content)
        return {"action": "write", "ok": ok, "key": key}
    if action == "read":
        return {"action": "read", "entries": read_entries(user_id, layer, key=key)}
    if action == "clear":
        return {"action": "clear", "removed": clear_entries(user_id, layer, key=key)}
    if action == "promote":
        if ctx is None:
            raise ValueError("promote requires a live app context (MCP call)")
        app = _get_ctx(ctx)
        mem = app.mm.user_memory(user_id) if layer == "user" else app.mm.agent_memory(user_id)
        keys = [key] if key else [e["key"] for e in read_entries(user_id, layer)]
        return {"action": "promote", **await promote_entries(mem, user_id, layer, keys=keys, to=to)}
    raise ValueError(f"unknown action: {action!r}")


async def memory_quality(
    action: Literal["report", "feedback"] = "report",
    entry_id: int = 0,
    useful: bool = True,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Memory quality metrics (D1.19): was_useful → score feedback loop.

    `feedback` applies one signal: useful → `recall_useful` audit row (feeds
    ACT-R frequency, D1.17) + importance boost +0.05 (cap 1.0); not useful →
    decay −0.05 (floor 0.05). `report` aggregates per-entry useful counts
    with current importance. Every adjustment is importance_audit-logged
    (reason='agent_feedback').
    """
    if ctx is not None:
        _get_ctx(ctx)
    _ = _validate_layer(layer)
    from features.quality import record_feedback, quality_report

    metrics.inc(METRIC_TOOL_CALLS)
    if action == "feedback":
        if not entry_id:
            raise ValueError("feedback requires entry_id")
        return {"action": "feedback", **await record_feedback(user_id, layer, int(entry_id), useful=bool(useful))}
    return {"action": "report", **await quality_report(user_id, layer)}


async def memory_counterfactual(
    action: Literal["save", "list"] = "list",
    anchor: str = "",
    premise: str = "",
    projection: str = "",
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Counterfactual memory (D1.20): "what could have been" notes.

    Branches from a real anchor (fact key, decision, episode ref): `premise`
    = what could have happened instead, `projection` = the expected outcome.
    Reflective material — saved explicitly, listed by anchor.
    """
    if ctx is not None:
        _get_ctx(ctx)
    _ = _validate_layer(layer)
    from features.counterfactual import save_cf, list_cfs

    metrics.inc(METRIC_TOOL_CALLS)
    if action == "save":
        if not anchor or not premise or not projection:
            raise ValueError("save requires anchor, premise and projection")
        cid = save_cf(user_id, anchor, premise, projection, layer=layer)
        return {"action": "save", "id": cid, "anchor": anchor}
    return {"action": "list", "counterfactuals": list_cfs(user_id, anchor=anchor)}


async def memory_recap(
    budget: int = 2000,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Session continuity recap (D1.2): the /new recovery pack.

    One-shot block list for a fresh session: last closed session (summary,
    topics, state deltas) → pending work (scratchpad, diff gaps, staged
    proposals) → markers → day digest, all within `budget` tokens. Call this
    instead of re-reading raw history (~2K tokens vs 50K). Same engine drives
    the `autohooks recap` CLI.
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    from .base import _get_memory

    mem = _get_memory(app, layer, user_id)
    from features.continuity import session_recap

    blocks = await session_recap(mem, user_id, budget=int(budget))
    metrics.inc(METRIC_TOOL_CALLS)
    return {"blocks": blocks, "count": len(blocks)}


async def memory_steering(
    query: str = "",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Steering hints (D1.3): route common intents to the best ariel tools.

    Deterministic route table + keyword intent match (max 3 hints); empty
    query returns the full table (boot-time system-prompt appendix). Ariel
    publishes the routes — the harness decides when to consult them.
    """
    if ctx is not None:
        _get_ctx(ctx)
    from features.steering import steering_hints

    metrics.inc(METRIC_TOOL_CALLS)
    hints = steering_hints(query)
    return {"hints": hints, "count": len(hints)}


async def memory_compress(
    text: str,
    mode: Literal["auto", "log", "code"] = "auto",
    max_lines: int = 50,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Tool-output compression (D1.4): shrink bulky text before re-injecting.

    mode="log" — build/test logs → errors only (+header, consecutive-duplicate
    collapse, cap); mode="code" — Python → skeleton (signatures kept, bodies
    dropped; non-Python falls back to log); "auto" — tries code first.
    Deterministic, no LLM.
    """
    if ctx is not None:
        _get_ctx(ctx)
    from features.compress_output import compress_output

    metrics.inc(METRIC_TOOL_CALLS)
    return compress_output(text, mode=mode, max_lines=int(max_lines))


async def memory_fact_blame(
    entry_id: int = 0,
    key: str = "",
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Provenance / fact blame (D1.6): who wrote this fact, when, and why.

    Rides the existing core_memory.source column (no migration): provenance
    ∈ {user_explicit, staging_promotion, episode_promotion, manual} (legacy).
    Returns the entry + importance_audit history (chunk_id = entry_id) +
    audit_log events (target_id = entry_id) — the evidence trail for
    debugging hallucinations. Lookup by entry_id or key.
    """
    if ctx is not None:
        _get_ctx(ctx)
    _ = _validate_layer(layer)
    from features.blame import fact_blame

    metrics.inc(METRIC_TOOL_CALLS)
    return {"action": "blame", **await fact_blame(user_id, layer, entry_id=int(entry_id), key=key)}


async def memory_query(
    source: Literal["core", "episodes"] = "core",
    importance_min: float | None = None,
    importance_max: float | None = None,
    key_like: str = "",
    content_like: str = "",
    created_since: float = 0.0,
    created_until: float = 0.0,
    tag: str = "",
    limit: int = 50,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Memory Query DSL (D1.7): structured analytics without raw SQL.

    Whitelisted filters → parameterized SQL over core_memory or episodes
    (importance band, key/content LIKE, created_at window, episode tag).
    Read-only, no injection surface (user SQL is never accepted).
    """
    if ctx is not None:
        _get_ctx(ctx)
    layer = _validate_layer(layer)
    from features.query_dsl import query_memory

    metrics.inc(METRIC_TOOL_CALLS)
    return await query_memory(
        user_id,
        layer=layer,
        source=source,
        importance_min=importance_min,
        importance_max=importance_max,
        key_like=key_like,
        content_like=content_like,
        created_since=created_since,
        created_until=created_until,
        tag=tag,
        limit=int(limit),
    )


async def memory_save_typed(
    type_name: str,
    fields: dict[str, str] | None = None,
    layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Typed memory schemas (D1.8): validate structured fields, store as L4.

    Built-in schemas: decision / error_pattern / relationship; custom ones
    in <data_dir>/schemas/*.yaml merge over them. Stored fact: key
    "<type>:<name>", metadata.typed = <type> — a typed knowledge base, not a
    bag of strings.
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    from .base import _get_memory

    mem = _get_memory(app, layer, user_id)
    from features.typed_memory import save_typed

    metrics.inc(METRIC_TOOL_CALLS)
    return {"action": "save_typed", **await save_typed(mem, user_id, type_name, dict(fields or {}))}


async def memory_load_rules(
    action: Literal["list", "reload"] = "list",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Memory rules engine (D1.9): declarative YAML rules for the write gate.

    Rules live at <data_dir>/rules.yaml: when_content_contains →
    importance_boost (sum, cap 0.3) + episode tags, applied in auto_save_text.
    `list` shows the active ruleset; `reload` re-reads the file (it is
    mtime-cached otherwise).
    """
    if ctx is not None:
        _get_ctx(ctx)
    from features.rules import load_rules

    metrics.inc(METRIC_TOOL_CALLS)
    rules = load_rules(force=(action == "reload"))
    return {"action": action, "rules": rules, "count": len(rules)}


async def memory_history(
    action: Literal["list", "get", "rollback", "snapshot_create", "snapshot_list", "snapshot_restore"] = "list",
    layer: str = "user",
    user_id: str = "default",
    key: str = "",
    history_id: int = 0,
    name: str = "",
    limit: int = 50,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Mutation ledger for L4 core facts (A2.2) + memory versioning (D1.14).

    list/get browse the ledger (slim/full rows with commit_hash and
    triggered_by provenance). rollback = git revert of ONE mutation
    (history_id): reinstates its pre-state. snapshot_create/list/restore
    manage named point-in-time captures of the (layer, user) fact set;
    restore is ledger-traced and idempotent. Scars stay forever.
    """
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    from features.history import get_history_row, list_history

    metrics.inc(METRIC_TOOL_CALLS)
    if action == "list":
        rows = await list_history(app.mm._cm, user_id, layer, key=key, limit=int(limit))
        return {"action": "list", "rows": rows, "count": len(rows)}
    if action == "get":
        row = await get_history_row(app.mm._cm, history_id=int(history_id))
        if row is None:
            raise ValueError(f"history_id not found: {history_id}")
        return {"action": "get", "row": row}
    if action == "rollback":
        if not history_id:
            raise ValueError("rollback requires history_id")
        from features.versioning import rollback

        return {"action": "rollback", **await rollback(app.mm._cm, int(history_id))}
    if action == "snapshot_create":
        if not name:
            raise ValueError("snapshot_create requires name")
        from features.versioning import snapshot_create

        return {"action": "snapshot_create", **await snapshot_create(app.mm._cm, layer, user_id, name)}
    if action == "snapshot_list":
        from features.versioning import snapshot_list

        return {"action": "snapshot_list", "snapshots": await snapshot_list(app.mm._cm, user_id, base_layer=layer)}
    if action == "snapshot_restore":
        if not name:
            raise ValueError("snapshot_restore requires name")
        from features.versioning import snapshot_restore

        return {"action": "snapshot_restore", **await snapshot_restore(app.mm._cm, layer, user_id, name)}
    raise ValueError(f"unknown action: {action!r}")


async def memory_branch(
    action: Literal["create", "write", "read", "diff", "merge", "delete", "list"] = "list",
    name: str = "",
    key: str = "",
    value: str = "",
    importance: float | None = None,
    keys: list[str] | None = None,
    base_layer: str = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Memory branches (D1.11): A/B persona staging for L4 facts.

    Branch = "<base>@<name>" namespace in core_memory; invisible to
    retrieval until merged. create = full clone; write = branch-local fact;
    diff = branch vs base; merge = cherry-pick keys (default: all differing)
    into base with branch_merge provenance; delete drops the branch.
    """
    app = _get_ctx(ctx)
    base_layer = _validate_layer(base_layer)
    from features import branches as br

    metrics.inc(METRIC_TOOL_CALLS)
    cm = app.mm._cm
    if action == "create":
        if not name:
            raise ValueError("create requires name")
        return {"action": "create", **await br.create_branch(cm, base_layer, user_id, name)}
    if action == "write":
        if not (name and key and value):
            raise ValueError("write requires name, key and value")
        return {"action": "write", **await br.write_branch(cm, base_layer, user_id, name, key, value, importance=importance)}
    if action == "read":
        return {"action": "read", "facts": await br.read_branch(cm, base_layer, user_id, name)}
    if action == "diff":
        return {"action": "diff", **await br.diff_branch(cm, base_layer, user_id, name)}
    if action == "merge":
        if not name:
            raise ValueError("merge requires name")
        return {"action": "merge", **await br.merge_branch(cm, base_layer, user_id, name, keys=keys)}
    if action == "delete":
        return {"action": "delete", **await br.delete_branch(cm, base_layer, user_id, name)}
    if action == "list":
        return {"action": "list", "branches": await br.list_branches(cm, user_id=user_id)}
    raise ValueError(f"unknown action: {action!r}")
