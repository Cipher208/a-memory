from __future__ import annotations

"""
Shared hook utilities — eliminates duplication between agent and user hooks.

All helpers are async by contract: the registry fires handlers on the
server's event loop and store APIs are async (sync bridges deadlocked
aiosqlite and silently dropped saves).
"""

import logging
import time
from typing import Any

from lifecycle.consolidation import ConsolidationEngine
from lifecycle.forgetting import ForgettingSystem
from rag.conflict import ConflictResolver
from rag.router import RetrievalRouter

from shared.constants import DB_NAME, DEFAULT_USER

logger = logging.getLogger(__name__)


async def forgetting_ritual(ctx: dict[str, Any]) -> dict[str, Any]:
    from config import config

    fs = ForgettingSystem()
    if not config.get("staging", "enabled", default=True):
        return await fs.cleanup()

    # C1.11: archive (destructive) is staged; decay + compress stay direct.
    conn = await fs._cm.get(DB_NAME)
    rows = await fs._find_archivable_entries(conn, time.time())
    ids = [r["entry_id"] for r in rows]
    staged: dict[str, Any] = {"decayed": await fs.decay_importance(), "compressed": await fs.compress_duplicates()}
    if not ids:
        staged["archived"] = 0
        return staged
    from features.staging import propose

    pid = await propose("forgetting", "archive", "default", "user", {"ids": ids})
    staged.update({"staged": True, "proposal_id": pid, "archived": 0})
    return staged


async def conflict_resolver(ctx: dict[str, Any], user_id: str = DEFAULT_USER) -> dict[str, Any]:
    content = ctx.get("content", "")
    resolver = ConflictResolver()
    return await resolver.check(user_id, content)


async def auto_context(ctx: dict[str, Any], user_id: str = DEFAULT_USER, layer: str | None = None) -> dict[str, Any]:
    query = ctx.get("query", "")
    router = RetrievalRouter(layer=layer, user_id=user_id) if layer is not None else RetrievalRouter(user_id=user_id)
    result = await router.route(query)
    return {"context": result.context, "strategy": result.strategy.value}


async def retrieval_router(
    ctx: dict[str, Any],
    user_id: str = DEFAULT_USER,
    layer: str | None = None,
    include_count: bool = False,
) -> dict[str, Any]:
    query = ctx.get("query", "")
    router = RetrievalRouter(layer=layer, user_id=user_id) if layer is not None else RetrievalRouter(user_id=user_id)
    result = await router.route(query)
    resp: dict[str, Any] = {
        "strategy": result.strategy.value,
        "confidence": result.confidence,
    }
    if include_count:
        resp["count"] = len(result.context)
    return resp


async def dream_buffer_staging(
    ctx: dict[str, Any],
    user_id: str = DEFAULT_USER,
    layer: str | None = None,
    cm: Any | None = None,
) -> dict[str, Any]:
    """Stage dream output into DreamBuffer for the hourly consolidation sweep."""
    from shared.dream_buffer import DreamBuffer

    content = ctx.get("summary") or ctx.get("text") or ""
    if not content:
        return {"action": "dream_buffer_skip", "reason": "empty"}

    buf = DreamBuffer(cm=cm, layer=layer or "user")
    row_id = await buf.add(user_id=user_id, session_id="dream", content=content[:2000], importance=float(ctx.get("importance", 0.5)))
    return {"action": "add_to_staging", "staging_id": row_id}


async def consolidation(
    ctx: dict[str, Any],
    user_id: str = DEFAULT_USER,
    min_importance: float | None = None,
    action_key: str | None = None,
) -> dict[str, Any]:
    staging_items = ctx.get("staging_items", [])
    final_key = action_key or "consolidated"
    from config import config

    if config.get("staging", "enabled", default=True):
        from features.staging import propose

        payload: dict[str, Any] = {"items": staging_items}
        if min_importance is not None:
            payload["min_importance"] = min_importance
        pid = await propose("consolidation", "consolidate_staging", user_id, "user", payload)
        return {"action": final_key, "staged": True, "proposal_id": pid, "promoted": 0, "skipped": 0}
    engine = ConsolidationEngine()
    if min_importance is not None:
        result = await engine.consolidate_staging(user_id, staging_items, min_importance=min_importance)
    else:
        result = await engine.consolidate_staging(user_id, staging_items)
    return {"action": final_key, **result}


async def cls_replay_hook(ctx: dict[str, Any], user_id: str = DEFAULT_USER) -> dict[str, Any]:
    """Nightly 2nd phase: boost L4 facts confirmed by recall in the window."""
    from features.replay import cls_replay
    from shared.connection import connection_manager

    layer = ctx.get("layer", "user")
    return await cls_replay(connection_manager, user_id, layer=layer)
