"""Deterministic 5-component session quality score (0-100).

Computed at close_session() time, stored in 2 new columns on the existing
sessions table (core/session.py). Failure to score is non-fatal — see
SessionStore.close_session.

5 components: depth / decision / linked_entries / user_engagement /
recall_usage. recall_usage counts dream() calls recorded in recall_events
(features/recall_telemetry.py) within the session window.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager

from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


def _component_formulas(
    *,
    message_count: int,
    duration_min: int,
    n_l4: int,
    n_l3: int,
    n_recall: int,
    n_topics: int,
    n_state_deltas: int,
) -> dict[str, float]:
    """Pure math for the 5 components. Each caps at 20; total caps at 100."""
    depth = min(10, message_count) + min(10, duration_min)
    decision = min(20, 4 * n_l4)
    linked_entries = min(20, 2 * n_l3)
    user_engagement = min(20, 5 * n_topics + 5 * n_state_deltas)
    recall_usage = min(20, 4 * n_recall)
    return {
        "depth": float(depth),
        "decision": float(decision),
        "linked_entries": float(linked_entries),
        "user_engagement": float(user_engagement),
        "recall_usage": float(recall_usage),
    }


async def _count_window(
    cm: AsyncConnectionManager,
    user_id: str,
    started_at: float,
    ended_at: float,
    table: str,
    col: str = "created_at",
) -> int:
    """Count rows in a single user-scoped time window. No layer filter (cross-layer).

    `table` is whitelisted by caller; the assertion is belt-and-suspenders
    against accidental string interpolation of untrusted input. `col` is the
    timestamp column (recall_events uses `timestamp`, stores use `created_at`).
    """
    assert table in {"episodes", "core_memory", "recall_events"}, f"unsupported count table: {table!r}"
    conn = await cm.get(DB_NAME)
    cursor = await conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE user_id=? AND {col} >= ? AND {col} <= ?",
        (user_id, started_at, ended_at),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def compute_session_quality(
    cm: AsyncConnectionManager,
    user_id: str,
    *,
    started_at: float,
    ended_at: float,
    message_count: int,
    topics: list[str],
    state_deltas: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    """Score a session by windowed counts + metadata. Returns (total, parts)."""
    # Guarantee recall_events exists even if no dream() ran this session;
    # otherwise the COUNT below would raise on a missing table and the
    # non-fatal wrapper in close_session would null the whole score.
    from features.recall_telemetry import ensure as ensure_recall_table

    await ensure_recall_table(cm)

    duration_min = max(0, int((ended_at - started_at) // 60))
    n_l4 = await _count_window(cm, user_id, started_at, ended_at, "core_memory")
    n_l3 = await _count_window(cm, user_id, started_at, ended_at, "episodes")
    n_recall = await _count_window(cm, user_id, started_at, ended_at, "recall_events", "timestamp")
    parts = _component_formulas(
        message_count=message_count,
        duration_min=duration_min,
        n_l4=n_l4,
        n_l3=n_l3,
        n_recall=n_recall,
        n_topics=len(topics),
        n_state_deltas=len(state_deltas),
    )
    total = float(sum(parts.values()))
    return total, parts


def parts_to_json(parts: dict[str, float]) -> str:
    """Serialize parts dict for SQLite column storage."""
    return json.dumps(parts, ensure_ascii=False, sort_keys=True)


def parts_from_json(blob: str | None) -> dict[str, float] | None:
    """Parse parts JSON; None on missing or invalid."""
    if not blob:
        return None
    try:
        data = json.loads(blob)
        if not isinstance(data, dict):
            return None
        return {k: float(v) for k, v in data.items()}
    except (ValueError, TypeError):
        return None
