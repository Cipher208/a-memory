from __future__ import annotations

"""
Shared hook utilities — eliminates duplication between agent and user hooks.
"""

import asyncio
import concurrent.futures
import logging
from typing import Any, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Coroutine

from lifecycle.consolidation import ConsolidationEngine
from lifecycle.forgetting import ForgettingSystem
from rag.conflict import ConflictResolver
from rag.router import RetrievalRouter

from shared.constants import DEFAULT_USER

logger = logging.getLogger(__name__)

# Module-level executor — max_workers=1 serializes DB access without blocking the event loop
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run async coroutine from sync context, handling running event loops.

    Uses ThreadPoolExecutor(max_workers=1) to serialize DB access
    without blocking the main event loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        future = _executor.submit(asyncio.run, coro)
        return future.result(timeout=30)
    return asyncio.run(coro)


def forgetting_ritual(ctx: dict[str, Any]) -> dict[str, Any]:
    fs = ForgettingSystem()
    return cast("dict[str, Any]", run_async(fs.cleanup()))


def conflict_resolver(ctx: dict[str, Any], user_id: str = DEFAULT_USER) -> dict[str, Any]:
    content = ctx.get("content", "")
    resolver = ConflictResolver()
    return cast("dict[str, Any]", run_async(resolver.check(user_id, content)))


def auto_context(ctx: dict[str, Any], user_id: str = DEFAULT_USER, layer: str | None = None) -> dict[str, Any]:
    query = ctx.get("query", "")
    router = RetrievalRouter(layer=layer, user_id=user_id) if layer is not None else RetrievalRouter(user_id=user_id)
    result = run_async(router.route(query))
    return {"context": result.context, "strategy": result.strategy.value}


def retrieval_router(
    ctx: dict[str, Any],
    user_id: str = DEFAULT_USER,
    layer: str | None = None,
    include_count: bool = False,
) -> dict[str, Any]:
    query = ctx.get("query", "")
    router = RetrievalRouter(layer=layer, user_id=user_id) if layer is not None else RetrievalRouter(user_id=user_id)
    result = run_async(router.route(query))
    resp: dict[str, Any] = {
        "strategy": result.strategy.value,
        "confidence": result.confidence,
    }
    if include_count:
        resp["count"] = len(result.context)
    return resp


def consolidation(
    ctx: dict[str, Any],
    user_id: str = DEFAULT_USER,
    min_importance: float | None = None,
    action_key: str | None = None,
) -> dict[str, Any]:
    staging = ctx.get("staging_items", [])
    engine = ConsolidationEngine()
    final_key = action_key or "consolidated"
    if min_importance is not None:
        result = run_async(engine.consolidate_staging(user_id, staging, min_importance=min_importance))
    else:
        result = run_async(engine.consolidate_staging(user_id, staging))
    return {"action": final_key, **result}
