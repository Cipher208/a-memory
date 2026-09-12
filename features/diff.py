"""Pure gap computation (C1.10 S5)."""

from __future__ import annotations

import sqlite3
from typing import Any

from shared.connection import connection_manager

_GAP_EVENTS = ("new_message", "auto_save_candidate")


def compute_session_gaps(mem: Any, since: float, until: float) -> list[dict[str, Any]]:
    """Return high-importance message ids dispatched but not persisted as expected.

    "Gap" definitions (v1, conservative — OR, not AND, between the two rules):
      - score >= 0.5 and saved_l3 == 0  → missing includes 'l3'  (L3 expected at threshold)
      - score >= 0.5 and saved_l4 == 0  → missing includes 'l4'  (L4 expected at threshold)

    Read-only over memory_dispatch_log. No DB writes.

    Preview source (fixed 2026-09-12): the dispatch log's own text_preview,
    written at dispatch time when the text is in hand. The old retroactive
    `mem.l3.get(source_msg_id)` lookup could never hit — source_msg_id is a
    harness conversation id, not an L3 episode id — so every gap carried an
    empty preview and 13,205 contentless diff_gap episodes accumulated in one
    base. Rows without preview are dropped: a marker with no content is noise.
    The `mem` param stays for signature compatibility (unused).
    """
    db_path = connection_manager.base_dir / "memory.db"
    if not db_path.exists():
        return []
    placeholders = ",".join("?" for _ in _GAP_EVENTS)
    sql = (
        f"SELECT id, source_msg_id, user_id, score, saved_l3, saved_l4, text_preview "
        f"FROM memory_dispatch_log "
        f"WHERE event IN ({placeholders}) AND created_at >= ? AND created_at < ? "
        f"ORDER BY id"
    )
    gaps: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(sql, (*_GAP_EVENTS, since, until)).fetchall()
    except sqlite3.OperationalError:
        # Pre-migration DB (no text_preview column): legacy rows are all the
        # empty-preview noise class anyway — no gaps rather than 13k markers.
        return []
    for _row_id, source_msg_id, user_id, score, saved_l3, saved_l4, text_preview in rows:
        missing: list[str] = []
        if score >= 0.5 and not saved_l3:
            missing.append("l3")
        if score >= 0.5 and not saved_l4:
            missing.append("l4")
        if not missing:
            continue
        preview = str(text_preview or "").strip()
        if not preview:
            continue
        gaps.append(
            {
                "source_msg_id": source_msg_id,
                "user_id": user_id,
                "score": float(score),
                "missing": missing,
                "text_preview": preview[:200],
            }
        )
    return gaps
