"""S18 gap-registry (3M Find Gap): open questions → nightly registry.

L3 episodes tagged question = open questions; plus repeated
zero-result queries (the S17 journal) = recall failures. The registry is a flat
table for proactive acquisition; the consumer is the nightly report.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

_GAP_MAX_AGE_DAYS = 7


async def _ensure_table(cm: Any) -> None:
    await cm.execute_script(
        DB_NAME,
        """
        CREATE TABLE IF NOT EXISTS memory_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL,
            gap TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'question',
            gap_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gaps_hash ON memory_gaps(gap_hash);
        """,
    )


def _gap_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.strip().lower().encode("utf-8", "ignore")).hexdigest()


async def build_registry(layer: str = "user", limit: int = 20) -> dict[str, Any]:
    """Collect fresh question episodes + zero-result tails into the registry. Best-effort.

    Idempotent by gap_hash: a repeated nightly run writes no duplicates.
    """
    cm = connection_manager
    await _ensure_table(cm)
    conn = await cm.get(DB_NAME)
    written = 0
    cutoff = time.time() - _GAP_MAX_AGE_DAYS * 86400

    async def _insert_gap(user_id: str, text: str, origin: str) -> None:
        """Idempotent insert (check-then-write; rowcount/total_changes are unreliable)."""
        h = _gap_hash(text)
        dup = await (await conn.execute("SELECT 1 FROM memory_gaps WHERE gap_hash=?", (h,))).fetchone()
        if dup:
            return
        await conn.execute(
            "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), layer, user_id, text[:300], origin, h),
        )
        nonlocal written
        written += 1

    try:
        # L3 questions: a nightly slice across ALL users (search_by_tag is a
        # per-user API; the registry is a cross-user aggregate).
        q_rows = await (
            await conn.execute(
                "SELECT user_id, summary FROM episodes"
                " WHERE layer=? AND created_at >= ? AND tags LIKE '%\"question\"%'"
                " ORDER BY created_at DESC LIMIT ?",
                (layer, cutoff, limit),
            )
        ).fetchall()
        for r in q_rows:
            text = str(r["summary"]).strip()
            if text:
                await _insert_gap(str(r["user_id"]), text, "question")
    except Exception:
        logger.exception("gap registry question scan failed")
    try:
        z_rows = await (
            await conn.execute(
                "SELECT user_id, query FROM recall_zero_results WHERE layer=? GROUP BY query_hash, user_id HAVING COUNT(*) >= 2 LIMIT ?",
                (layer, limit),
            )
        ).fetchall()
        for r in z_rows:
            text = str(r["query"]).strip()
            if text:
                await _insert_gap(str(r["user_id"]), text, "zero_result")
        await conn.commit()
    except Exception:
        # recall_zero_results is a lazy-ensure table (the S17 miner); until the
        # first nightly run it does not exist — the zero-result branch just stays silent.
        logger.debug("gap registry zero-result scan skipped: no journal yet")
    rows = await (await conn.execute("SELECT ts, user_id, gap, origin FROM memory_gaps ORDER BY id DESC LIMIT ?", (limit,))).fetchall()
    return {"gaps": [dict(r) for r in rows], "written": written}
