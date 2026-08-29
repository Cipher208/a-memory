"""Compaction drift log + rehydrate lookups (D3.5 S3).

Sync sqlite3 over connection_manager.base_dir (same pattern as the
memory_dispatch_log inserts in hooks/external.py). All helpers are
best-effort: a missing/mis-migrated table degrades to False/None —
compaction bookkeeping must never break the dispatch path.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


def _db_path() -> Any:
    from shared.connection import connection_manager

    return connection_manager.base_dir / "memory.db"


def log_compaction(
    user_id: str,
    old_session_id: str | None = None,
    new_session_id: str | None = None,
    reason: str | None = None,
    summary: str | None = None,
) -> bool:
    """Insert one compaction_events row. Returns False on any failure."""
    try:
        with sqlite3.connect(str(_db_path())) as conn:
            conn.execute(
                "INSERT INTO compaction_events (user_id, old_session_id, new_session_id, reason, summary, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, old_session_id, new_session_id, reason, (summary or "")[:2000], time.time()),
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.debug("compaction_events insert failed: %s", exc)
        return False


def rehydrate_enabled() -> bool:
    """Gate knob: rehydrate.enabled (default true)."""
    from config import config

    return bool(config.get("rehydrate", "enabled", default=True))


def recent_compaction(user_id: str, window_hours: float) -> dict[str, Any] | None:
    """Newest compaction row for user within the window, or None."""
    cutoff = time.time() - window_hours * 3600
    try:
        with sqlite3.connect(str(_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM compaction_events WHERE user_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (user_id, cutoff),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.debug("compaction_events read failed: %s", exc)
        return None
