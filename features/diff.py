"""Pure gap computation (C1.10 S5)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from shared.connection import connection_manager
from shared.sync_db import open_db

_GAP_EVENTS = ("new_message", "auto_save_candidate")

# Fallbacks, mirroring the predicate the migration ships as `auto_save_default`
# (`{"min_importance": 0.5, "l4_min_importance": 0.8}`). Used only when a trigger has no
# enabled watch rule, or the rule omits the key.
_DEFAULT_L3_MIN = 0.5
_DEFAULT_L4_MIN = 0.8


def _declared_thresholds(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """Read the two-tier thresholds per trigger from the DECLARED watch rules.

    This house declares them in `watch_rules.predicate` — two different numbers for two
    different tiers (`min_importance` 0.5 for L3, `l4_min_importance` 0.8 for L4), and
    `mcp_server/tools/ops.py::_validate_predicate` is their validator. The gap
    calculation used to carry a single hard-coded 0.5 and apply it to BOTH branches, so
    every dispatch scoring in the 0.5-0.8 band was reported as a missing L4. That band
    is exactly where no write is expected: measured on one live base, **313 of 439**
    `diff_gap` episodes (71%) came from it, and only 126 named a real expectation.

    A rule that omits a key inherits the default for it; an event with no enabled rule
    gets both defaults. Mirroring the validator's bounds check is deliberate: a value
    outside [0, 1] is treated as absent rather than allowed to invert every comparison.
    """
    out: dict[str, tuple[float, float]] = {}
    try:
        rows = conn.execute("SELECT trigger, predicate FROM watch_rules WHERE enabled = 1").fetchall()
    except sqlite3.OperationalError:
        # Pre-migration base: no rules table, so both defaults apply.
        return out
    for trigger, raw in rows:
        try:
            pred = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(pred, dict):
            continue
        try:
            l3_min = float(pred.get("min_importance", _DEFAULT_L3_MIN))
            l4_min = float(pred.get("l4_min_importance", _DEFAULT_L4_MIN))
        except (TypeError, ValueError):
            continue
        if 0 <= l3_min <= 1 and 0 <= l4_min <= 1:
            out[str(trigger)] = (l3_min, l4_min)
    return out


def compute_session_gaps(mem: Any, since: float, until: float, *, user_id: str) -> list[dict[str, Any]]:
    """Return high-importance message ids dispatched but not persisted as expected.

    "Gap" definitions (v1, conservative — OR, not AND, between the two rules), each
    measured against its OWN tier threshold as declared in `watch_rules`:
      - score >= min_importance     and saved_l3 == 0 → missing includes 'l3'
      - score >= l4_min_importance  and saved_l4 == 0 → missing includes 'l4'

    Read-only over memory_dispatch_log. No DB writes. Only the caller's own
    dispatches are read: an id addressed by scope carries (user_id, layer) from
    the caller, never from the row.

    Preview source (fixed 2026-09-12): the dispatch log's own text_preview,
    written at dispatch time when the text is in hand. The old retroactive
    `mem.l3.get(source_msg_id)` lookup could never hit — source_msg_id is a
    harness conversation id, not an L3 episode id — so every gap carried an
    empty preview and 13,205 contentless diff_gap episodes accumulated in one
    base. Rows without preview are dropped: a marker with no content is noise.
    The `mem` param stays for signature compatibility (unused).

    Threshold source (fixed 2026-10-12): see `_declared_thresholds`. The L4 branch
    previously used the L3 number, which manufactured gaps out of normal routing.
    """
    db_path = connection_manager.base_dir / "memory.db"
    if not db_path.exists():
        return []
    placeholders = ",".join("?" for _ in _GAP_EVENTS)
    sql = (
        f"SELECT id, event, source_msg_id, user_id, score, saved_l3, saved_l4, text_preview "
        f"FROM memory_dispatch_log "
        f"WHERE event IN ({placeholders}) AND created_at >= ? AND created_at < ? AND user_id = ? "
        f"ORDER BY id"
    )
    gaps: list[dict[str, Any]] = []
    try:
        with open_db(db_path) as conn:
            thresholds = _declared_thresholds(conn)
            rows = conn.execute(sql, (*_GAP_EVENTS, since, until, user_id)).fetchall()
    except sqlite3.OperationalError:
        # Pre-migration DB (no text_preview column): legacy rows are all the
        # empty-preview noise class anyway — no gaps rather than 13k markers.
        return []
    for _row_id, event, source_msg_id, user_id, score, saved_l3, saved_l4, text_preview in rows:
        l3_min, l4_min = thresholds.get(str(event), (_DEFAULT_L3_MIN, _DEFAULT_L4_MIN))
        missing: list[str] = []
        if score >= l3_min and not saved_l3:
            missing.append("l3")
        if score >= l4_min and not saved_l4:
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
