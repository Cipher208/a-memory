"""Compact-to-budget: evict lowest-activation L4 facts down to a budget.

Eviction = ARCHIVAL (never bare deletion): rows move to archived_memories via
ForgettingSystem.archive_entries, so they stay restorable. never_archive types
(instruction/rule/commitment) are always exempt.

Activation proxy (core_memory has no access_count column, so the ACT-R
frequency term is unavailable — importance+recency stand-in):
    activation = importance * exp(-decay_rate(kind) * age_days)
using the same per-kind decay_rate as lifecycle/forgetting.py::decay_importance.
Old low-importance rows evict first.
"""

from __future__ import annotations

import math
import time

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.memory_types import MemoryKind, get_policy
from shared.write_dispatch import get_write_dispatch


def _activation_proxy(importance: float, memory_kind: str | None, updated_at: float, now: float) -> float:
    """ACT-R stand-in: importance * exp(-decay_rate * age_days)."""
    age_days = max(0.0, (now - float(updated_at)) / 86400.0)
    decay_rate = get_policy(memory_kind or "fact").decay_rate
    return float(importance) * math.exp(-decay_rate * age_days)


async def compact_under_budget(
    user_id: str,
    layer: str,
    budget: int = 500,
    cm: AsyncConnectionManager | None = None,
) -> dict[str, int]:
    """Evict lowest-activation facts (via archive) until layer/user fits budget."""
    dispatch = get_write_dispatch(cm or connection_manager)
    count = int(await dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=?", (layer, user_id)) or 0)
    if count <= budget:
        return {"evicted": 0, "remaining": count}

    protected = [k.value for k in MemoryKind if get_policy(k).never_archive]
    placeholders = ",".join(["?"] * len(protected))
    rows = await dispatch.query(
        DB_NAME,
        f"""SELECT entry_id, memory_kind, importance, updated_at FROM core_memory
            WHERE layer=? AND user_id=?
              AND (memory_kind IS NULL OR memory_kind NOT IN ({placeholders}))""",
        (layer, user_id, *protected),
    )

    now = time.time()
    scored = sorted(
        ((_activation_proxy(r["importance"], r["memory_kind"], r["updated_at"], now), int(r["entry_id"])) for r in rows),
    )
    evict_ids = [entry_id for _, entry_id in scored[: count - budget]]
    if not evict_ids:
        return {"evicted": 0, "remaining": count}

    from lifecycle.forgetting import ForgettingSystem

    evicted = await ForgettingSystem(cm=cm or connection_manager, layer=layer).archive_entries(evict_ids)
    remaining = await dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=?", (layer, user_id))
    return {"evicted": evicted, "remaining": int(remaining) if remaining is not None else count - evicted}
