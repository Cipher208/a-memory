"""Rejection log for the intake door — the counter the sixth bug needed.

WHY THIS EXISTS

`memory_dispatch_log` records what was SAVED. Nothing recorded what was REFUSED.
Every guard in `auto_save_text` and every block in the middleware pipeline returns
before the dispatch-log insert, so a refused message left no row anywhere: not in
`l0_journal` (the door runs before capture), not in `memory_dispatch_log`, not in
a warning. Measured on a live base, that table holds 1419 rows and **not one has
`score = 0.0`** — the refusals are simply absent.

That silence cost real data. The sixth bug was `_TRANSCRIPT_HEAD` carrying the
whole markdown set, so the door discarded 624 of one persona's 790 messages —
79.7% of her voice — and the only way to find it was to re-derive the number by
hand from the source database. A counter would have shown it as a rate change:
one or two refusals a day, then hundreds inside one window.

THE RULE THIS ENCODES

Six bugs in this repository shared one shape: a refusal reported by VALUE, which
its caller did not read, equals silence. `rate_limited()` fixed that for one
caller. This module fixes it for the operator: the refusal is written down where
someone can find it, with enough text to judge whether the guard was right.

WHAT IS NOT STORED

The refused text is NOT stored — only a short preview (the point is to identify
the refusal, not to keep the message; keeping it would put back exactly what the
guard refused to keep). The preview lives in its own table that nothing reads for
recall, so it cannot poison L3/L4 retrieval.

WHY A PRIVATE SYNCHRONOUS CONNECTION

Writes go through a short-lived `sqlite3` connection, not `connection_manager`,
for the same reason the dispatch-log insert does: this is best-effort
observability beside the pipeline, and it must not join — or worse, commit — the
transaction that `shared.l0.capture` is holding open around its hash-chain write.
A shared aiosqlite connection would let a stray `commit()` land in the middle of
that chain update. A one-second timeout keeps a locked database from ever
stalling the message path.

Volume is bounded by the source, not by the guard: one row per refusal, and in
normal operation refusals run at one or two a day. A `below_importance_threshold`
row is NOT a bug report — it is the designed filter doing its job — which is why
every row carries its reason and the reader groups by it.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

#: How much of a refused message is kept for identification. Long enough to
#: recognise the text, short enough that a stored preview of something refused
#: can never be mistaken for the message itself.
PREVIEW_CHARS = 160

#: Short, stable tags. The reader groups by these, so they are a contract: the
#: middleware reports `"Rate limit exceeded (100/min)"` and the door reports
#: `"transcript"`, and both have to land in one vocabulary or the counts split.
REASON_DUMP = "transcript"
REASON_HARNESS_LIMIT = "harness_limit"
REASON_SYSTEM_INJECTION = "system_injection"
REASON_DUPLICATE = "duplicate_l0_block"
REASON_RATE_LIMIT = "rate_limit"
REASON_IMPORTANCE = "below_importance_threshold"
REASON_INVALID = "invalid_input"
#: A tool's own status output (the self-monitoring digest, the cleaner's summary)
#: refused by `lifecycle.distiller` before any atom is written. It belongs in this
#: vocabulary, not in a counter of its own: the reader groups by reason, and a
#: second place to count is how the counts split. Measured on one live base: 42
#: rows, 216 episodes that were never memories.
REASON_MACHINE_REPORT = "machine_report"
REASON_UNKNOWN = "unknown"

#: Middleware block reasons are free-form sentences built at the site that
#: blocks. Mapping them here keeps the naming in one place — the alternative,
#: matching on prefixes at every reader, is how the six bugs happened.
_MIDDLEWARE_REASONS: tuple[tuple[str, str], ...] = (
    ("Rate limit exceeded", REASON_RATE_LIMIT),
    ("below_importance_threshold", REASON_IMPORTANCE),
    ("user_id is required", REASON_INVALID),
    ("key is required", REASON_INVALID),
)


def normalize_reason(reason: str) -> str:
    """Fold a free-form block reason onto the stable tag vocabulary.

    Unknown reasons are returned unchanged rather than dropped: a new block site
    must show up in the counts as itself, not vanish into "unknown".
    """
    text = (reason or "").strip()
    if not text:
        return REASON_UNKNOWN
    for prefix, tag in _MIDDLEWARE_REASONS:
        if text.startswith(prefix):
            return tag
    return text


def _db_path() -> str:
    return str(connection_manager.base_dir / DB_NAME)


async def record_rejection(
    reason: str,
    *,
    layer: str,
    user_id: str,
    event: str = "",
    source_msg_id: int | None = None,
    text: str = "",
) -> None:
    """Write one refusal row. Never raises — logging a refusal must not cause one.

    Best-effort by contract, like `shared.l0.capture`: if the table is missing
    (a pre-migration database) or the write fails, the pipeline continues. The
    alternative — letting observability break the flow it observes — would make
    this guard a new source of the exact silent loss it exists to expose.
    """
    try:
        with sqlite3.connect(_db_path(), timeout=1.0) as conn:
            conn.execute(
                "INSERT INTO memory_dispatch_rejections "
                "(event, source_msg_id, layer, user_id, reason, text_preview, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event or "",
                    source_msg_id,
                    (layer or "user"),
                    (user_id or "default"),
                    normalize_reason(reason),
                    (text or "")[:PREVIEW_CHARS],
                    time.time(),
                ),
            )
    except Exception as exc:
        logger.debug("rejection log write failed: %s", exc)


async def rejection_summary(*, user_id: str | None = None, since: float | None = None) -> dict[str, Any]:
    """Count refusals per reason, newest window first. Never raises; empty on failure."""
    try:
        with sqlite3.connect(_db_path(), timeout=1.0) as conn:
            where: list[str] = []
            params: list[Any] = []
            if user_id:
                where.append("user_id = ?")
                params.append(user_id)
            if since is not None:
                where.append("created_at >= ?")
                params.append(since)
            clause = f" WHERE {' AND '.join(where)}" if where else ""
            rows = conn.execute(
                f"SELECT reason, layer, COUNT(*) FROM memory_dispatch_rejections{clause} GROUP BY reason, layer ORDER BY COUNT(*) DESC",
                tuple(params),
            ).fetchall()
    except Exception:
        return {"total": 0, "by_reason": {}, "by_layer": {}, "available": False}

    by_reason: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    total = 0
    for reason, layer, count in rows:
        by_reason[str(reason)] = by_reason.get(str(reason), 0) + int(count)
        by_layer[str(layer)] = by_layer.get(str(layer), 0) + int(count)
        total += int(count)
    return {"total": total, "by_reason": by_reason, "by_layer": by_layer, "available": True}


async def recent_rejections(*, limit: int = 20, user_id: str | None = None) -> list[dict[str, Any]]:
    """Return the newest refusals with their previews — the part a count alone cannot give.

    The count says something changed; the preview says whether the guard was
    right. That is the difference between "591 refusals" (investigate) and
    seeing hundreds of lines of the persona's own italic prose (the guard is
    wrong).
    """
    try:
        with sqlite3.connect(_db_path(), timeout=1.0) as conn:
            sql = "SELECT reason, layer, event, source_msg_id, text_preview, created_at FROM memory_dispatch_rejections"
            params: list[Any] = []
            if user_id:
                sql += " WHERE user_id = ?"
                params.append(user_id)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, int(limit)))
            rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception:
        return []

    return [
        {
            "reason": str(r[0]),
            "layer": str(r[1]),
            "event": str(r[2]),
            "source_msg_id": r[3],
            "preview": str(r[4]),
            "created_at": r[5],
        }
        for r in rows
    ]
