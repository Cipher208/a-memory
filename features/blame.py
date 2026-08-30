"""D1.6 provenance — fact blame: who wrote this fact, when, and why.

Rides the EXISTING core_memory.source column (no migration): canonical
provenance values are user_explicit (agent/user wrote it deliberately),
staging_promotion / episode_promotion (consolidation products, B1.4) and
the legacy manual default; emotion_trigger / wiki_import / graph_inference
are reserved until such core_memory write paths exist. Evidence trail =
importance_audit rows (chunk_id = entry_id) + audit_log rows
(target_id = str(entry_id)).
"""

from __future__ import annotations

import json
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME


async def fact_blame(user_id: str, layer: str, entry_id: int = 0, key: str = "") -> dict[str, Any]:
    """Assemble the evidence trail for one core_memory fact."""
    conn = await connection_manager.get(DB_NAME)
    if entry_id:
        row = await (
            await conn.execute(
                "SELECT * FROM core_memory WHERE entry_id=? AND layer=? AND user_id=?",
                (int(entry_id), layer, user_id),
            )
        ).fetchone()
    elif key:
        row = await (
            await conn.execute(
                "SELECT * FROM core_memory WHERE key=? AND layer=? AND user_id=?",
                (key, layer, user_id),
            )
        ).fetchone()
    else:
        raise ValueError("blame requires entry_id or key")
    if row is None:
        raise ValueError(f"entry not found: entry_id={entry_id} key={key!r}")
    eid = int(row["entry_id"])

    audit_rows = await (
        await conn.execute(
            "SELECT source, old_importance, new_importance, reason, rescored_at FROM importance_audit"
            " WHERE user_id=? AND chunk_id=? ORDER BY rescored_at",
            (user_id, eid),
        )
    ).fetchall()
    log_rows = await (
        await conn.execute(
            "SELECT action, layer, details, timestamp FROM audit_log WHERE user_id=? AND target_id=? ORDER BY timestamp",
            (user_id, str(eid)),
        )
    ).fetchall()

    history = [
        {
            "source": r["source"],
            "old": r["old_importance"],
            "new": r["new_importance"],
            "reason": r["reason"],
            "at": r["rescored_at"],
        }
        for r in audit_rows
    ]
    events = []
    for r in log_rows:
        with_context = json.loads(r["details"] or "{}") if isinstance(r["details"], str) else {}
        events.append({"action": r["action"], "layer": r["layer"], "details": with_context, "at": r["timestamp"]})

    return {
        "entry_id": eid,
        "layer": row["layer"],
        "key": row["key"],
        "value": row["value"],
        "importance": row["importance"],
        "provenance": row["source"] or "manual",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "importance_history": history,
        "audit_events": events,
        "counts": {"importance_changes": len(history), "audit_events": len(events)},
    }
