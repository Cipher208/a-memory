"""D1.20 counterfactual memory — "what could have been" notes.

A counterfactual branches from a real anchor (a fact key, a decision, an
episode ref): premise = what could have happened instead, projection = the
expected outcome. Reflective material — saved by the agent, listed by anchor.
Sync sqlite3 over connection_manager (rehydrate.py pattern); missing table
degrades gracefully.
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


def save_cf(user_id: str, anchor: str, premise: str, projection: str, layer: str = "user") -> int:
    """Insert one counterfactual row. Returns the row id (0 on failure)."""
    try:
        with sqlite3.connect(str(_db_path())) as conn:
            cursor = conn.execute(
                "INSERT INTO counterfactuals (user_id, layer, anchor, premise, projection, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, layer, anchor, premise, projection, time.time()),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)
    except Exception as exc:
        logger.debug("counterfactuals insert failed: %s", exc)
        return 0


def list_cfs(user_id: str, anchor: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """Counterfactuals newest-first; filtered by anchor when given."""
    try:
        with sqlite3.connect(str(_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            if anchor:
                rows = conn.execute(
                    "SELECT * FROM counterfactuals WHERE user_id=? AND anchor=? ORDER BY created_at DESC LIMIT ?",
                    (user_id, anchor, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM counterfactuals WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("counterfactuals read failed: %s", exc)
        return []
