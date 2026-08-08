import logging
from typing import Any

from shared.saga.engine import SagaStep

logger = logging.getLogger(__name__)


async def _consolidation_gather(ctx: dict[str, Any]) -> dict[str, Any]:
    """Gather recent memories from L1 (staging)."""
    mm = ctx.get("_mm")
    user_id = ctx.get("user_id")
    if not mm or not user_id:
        return {"gathered_count": 0, "error": "missing_manager_or_user"}

    # In Ariel, L1 is usually represented by a specific store or 'recent' memories
    # For this implementation, we assume mm has a method to get recent/l1 items
    try:
        # We simulate gathering from l1.
        # In a real scenario, this would call mm.get_l1_memories(user_id)
        memories = await mm.search(user_id, "", limit=20)
        ctx["staging_items"] = memories
        return {"gathered_count": len(memories)}
    except Exception as e:
        logger.exception(f"Failed to gather memories: {e}")
        return {"gathered_count": 0, "error": str(e)}


async def _consolidation_distill(ctx: dict[str, Any]) -> dict[str, Any]:
    """Filter gathered memories by importance (distillation)."""
    items = ctx.get("staging_items", [])
    # Distill: keep only items with importance > 0.7
    important_items = [i for i in items if i.get("importance", 0) >= 0.7]
    ctx["important_items"] = important_items
    return {"distilled_count": len(important_items)}


async def _consolidation_promote(ctx: dict[str, Any]) -> dict[str, Any]:
    """Promote important items to L2 (Core Memory)."""
    mm = ctx.get("_mm")
    user_id = ctx.get("user_id")
    items = ctx.get("important_items", [])

    if not mm or not user_id:
        return {"promoted_count": 0}

    promoted_keys = []
    for item in items:
        key = item.get("key")
        value = item.get("value")
        importance = item.get("importance", 0.8)

        # Save to Core Memory (L2)
        await mm.save(user_id=user_id, key=key, value=value, importance=importance, memory_kind="fact", source="consolidation")
        promoted_keys.append(key)

    ctx["promoted_keys"] = promoted_keys
    return {"promoted_count": len(promoted_keys)}


async def _consolidation_rollback(ctx: dict[str, Any]) -> None:
    """Rollback promoted items (forget them from L2)."""
    mm = ctx.get("_mm")
    user_id = ctx.get("user_id")
    keys = ctx.get("promoted_keys", [])

    if not mm or not user_id:
        return

    for key in keys:
        try:
            await mm.delete(user_id, key)
            logger.info(f"Rolled back promoted memory: {key}")
        except Exception as e:
            logger.exception(f"Failed to rollback memory {key}: {e}")


def create_consolidation_saga(user_id: str, mm: Any) -> list[SagaStep]:
    """
    Returns steps for the memory consolidation saga.
    Logic: Gather (L1) -> Distill (Filter) -> Promote (L2).
    Compensation: Rollback promoted items (forget).
    """
    return [
        SagaStep(name="gather_memories", action=_consolidation_gather),
        SagaStep(name="distill_memories", action=_consolidation_distill),
        SagaStep(name="promote_to_core", action=_consolidation_promote, compensation=_consolidation_rollback),
    ]
