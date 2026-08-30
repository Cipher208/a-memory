"""D1.19 memory quality metrics — was_useful → score feedback loop.

The agent confirms or denies recalled memories (`memory_quality` tool):
useful → an audit_log `recall_useful` row (feeds ACT-R frequency, D1.17) plus
a gentle importance boost (+0.05, cap 1.0); not useful → decay (−0.05,
floor 0.05). Every adjustment writes an importance_audit row
(reason='agent_feedback') — same write pattern as the CLS replay (D1.18).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

BOOST = 0.05
FLOOR = 0.05


def _db_path() -> Any:
    from shared.connection import connection_manager

    return connection_manager.base_dir / "memory.db"


async def record_feedback(user_id: str, layer: str, entry_id: int, useful: bool) -> dict[str, Any]:
    """Apply one was_useful signal: audit row + importance adjust."""
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT importance FROM core_memory WHERE entry_id=? AND user_id=?",
            (int(entry_id), user_id),
        ).fetchone()
        if row is None:
            return {"status": "not_found", "entry_id": int(entry_id)}
        old = float(row["importance"])
        new = min(1.0, old + BOOST) if useful else max(FLOOR, old - BOOST)
        now = time.time()
        conn.execute("UPDATE core_memory SET importance=?, updated_at=? WHERE entry_id=?", (new, now, int(entry_id)))
        conn.execute(
            "INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, signal_breakdown, reason, rescored_at)"
            " VALUES (?, ?, 'core_memory', ?, ?, '{}', 'agent_feedback', ?)",
            (user_id, int(entry_id), old, new, now),
        )
        if useful:
            # confirmed-useful recall = frequency signal for ACT-R (D1.17)
            conn.execute(
                "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp)"
                " VALUES (?, 'recall_useful', 'core_memory', ?, '{}', ?)",
                (user_id, str(int(entry_id)), now),
            )
        else:
            conn.execute(
                "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp)"
                " VALUES (?, 'agent_feedback_neg', 'core_memory', ?, '{}', ?)",
                (user_id, str(int(entry_id)), now),
            )
        conn.commit()
        return {"status": "ok", "entry_id": int(entry_id), "old": old, "new": new, "useful": useful}
    except Exception as exc:
        logger.debug("quality feedback failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    finally:
        conn.close()


async def quality_report(user_id: str, layer: str, limit: int = 10) -> dict[str, Any]:
    """Aggregate was_useful signals per entry: frequency + feedback balance."""
    try:
        conn = sqlite3.connect(str(_db_path()))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT c.entry_id, c.key, c.importance,
                      SUM(CASE WHEN a.action='recall_useful' THEN 1 ELSE 0 END) AS useful_count,
                      SUM(CASE WHEN a.action='agent_feedback_neg' THEN 1 ELSE 0 END) AS neg_count,
                      MAX(a.timestamp) AS last_signal
               FROM audit_log a
               JOIN core_memory c ON c.entry_id = CAST(a.target_id AS INTEGER)
               WHERE a.user_id=? AND a.layer='core_memory'
                 AND a.action IN ('recall_useful', 'agent_feedback_neg')
               GROUP BY c.entry_id
               ORDER BY useful_count DESC, last_signal DESC
               LIMIT ?""",
            (user_id, int(limit)),
        ).fetchall()
        counts = conn.execute(
            "SELECT COUNT(DISTINCT target_id) AS n FROM audit_log"
            " WHERE user_id=? AND layer='core_memory' AND action IN ('recall_useful','agent_feedback_neg')",
            (user_id,),
        ).fetchone()
        conn.close()
        return {
            "total_tracked": int(counts["n"]) if counts else 0,
            "top_useful": [dict(r) for r in rows],
        }
    except Exception as exc:
        logger.debug("quality report failed: %s", exc)
        return {"total_tracked": 0, "top_useful": [], "error": str(exc)}
