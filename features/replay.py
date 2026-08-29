"""Recall confirmation signal + CLS replay (nightly L4 boost).

record_recall_useful: dream writes one audit_log row per recalled core fact
(action='recall_useful', target_id=str(entry_id)) — the frequency signal
consumed by ACT-R activation and ImportanceScheduler. Written directly to
audit_log (not via AuditTrail.log) so the signal survives even when the
audit_trail feature flag is off.
cls_replay: nightly 2nd phase — boosts facts confirmed by recall within the
window (Complementary Learning Systems: hippocampal replay consolidates
reactivated traces into neocortex).
"""

from __future__ import annotations

import json
import time
from typing import Any

from shared.constants import DB_NAME


async def record_recall_useful(cm: Any, layer: str, user_id: str, entries: list[tuple[int, str]]) -> int:
    """Record one recall_useful row per (entry_id, key). Returns rows written."""
    if not entries:
        return 0
    conn = await cm.get(DB_NAME)
    now = time.time()
    await conn.executemany(
        "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?, 'recall_useful', 'core_memory', ?, ?, ?)",
        [(user_id, str(entry_id), json.dumps({"key": key}), now) for entry_id, key in entries],
    )
    await conn.commit()
    return len(entries)


async def cls_replay(cm: Any, user_id: str, layer: str = "user", window_hours: int = 24, boost: float = 0.05) -> dict[str, int]:
    """Boost L4 facts recalled within the window. Returns counters."""
    conn = await cm.get(DB_NAME)
    cutoff = time.time() - window_hours * 3600
    rows = await (
        await conn.execute(
            """SELECT entry_id, importance FROM core_memory
               WHERE layer=? AND user_id=? AND importance < 1.0
                 AND entry_id IN (
                     SELECT DISTINCT CAST(target_id AS INTEGER) FROM audit_log
                     WHERE action='recall_useful' AND layer='core_memory' AND timestamp > ?
                 )""",
            (layer, user_id, cutoff),
        )
    ).fetchall()
    boosted = 0
    now = time.time()
    for r in rows:
        old = float(r["importance"])
        new = min(1.0, old + boost)
        if new <= old:
            continue
        await conn.execute("UPDATE core_memory SET importance=?, updated_at=? WHERE entry_id=?", (new, now, int(r["entry_id"])))
        await conn.execute(
            """INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, signal_breakdown, reason, rescored_at)
               VALUES (?, ?, 'core_memory', ?, ?, '{}', 'cls_replay', ?)""",
            (user_id, int(r["entry_id"]), old, new, now),
        )
        boosted += 1
    await conn.commit()
    return {"boosted": boosted}
