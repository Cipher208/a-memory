"""Recall telemetry — record every dream() call for usage analytics.

Every dream() call inserts a row into recall_events (query, intent,
result_count, layer, user_id, timestamp). Counts feed memory_stats.recall_count
and the 5th session-quality component (recall_usage).

Kept separate from temporal_events so the timeline shown by
dream(intent='recent') stays clean: a recall is an ephemeral "agent asked"
operation, not a durable "something happened" event.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from shared.constants import DB_NAME

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager


async def ensure(cm: AsyncConnectionManager) -> None:
    """Idempotent schema setup. Mirror of other features/* stores."""
    await cm.execute_script(
        DB_NAME,
        """
        CREATE TABLE IF NOT EXISTS recall_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL,
            query TEXT NOT NULL,
            intent TEXT NOT NULL DEFAULT 'balanced',
            result_count INTEGER DEFAULT 0,
            timestamp REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recall_user ON recall_events(user_id);
        CREATE INDEX IF NOT EXISTS idx_recall_time ON recall_events(timestamp);
        """,
    )


async def record_recall(
    cm: AsyncConnectionManager,
    layer: str,
    user_id: str,
    query: str,
    intent: str,
    result_count: int,
) -> int:
    """Insert a recall record and return its event_id."""
    await ensure(cm)
    conn = await cm.get(DB_NAME)
    cursor = await conn.execute(
        "INSERT INTO recall_events (layer, user_id, query, intent, result_count, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (layer, user_id, query[:500], intent, result_count, time.time()),
    )
    await conn.commit()
    return int(cursor.lastrowid or 0)


async def count_recalls(
    cm: AsyncConnectionManager,
    user_id: str,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> int:
    """Count recall records, optionally scoped to a [started_at, ended_at] window.

    Cross-layer: user_id scope only (a session window counts both user and
    agent recalls). Idempotent ensure() guards against a missing table.
    """
    await ensure(cm)
    conn = await cm.get(DB_NAME)
    sql = "SELECT COUNT(*) FROM recall_events WHERE user_id=?"
    params: list[Any] = [user_id]
    if started_at is not None and ended_at is not None:
        sql += " AND timestamp >= ? AND timestamp <= ?"
        params.extend([started_at, ended_at])
    row = await (await conn.execute(sql, tuple(params))).fetchone()
    return int(row[0]) if row else 0
