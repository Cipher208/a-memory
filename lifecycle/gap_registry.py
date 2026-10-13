"""S18 gap-registry (3M Find Gap): open questions → nightly registry → the brief.

L3 episodes tagged question = open questions; plus repeated zero-result queries (the S17
journal) = recall failures. The nightly phase (`graph_enrich`) fills a flat table; the
read side is `open_gaps`, and its surface is the session brief (`features.continuity`),
where the agent already reads what is pending — NOT the nightly report alone, which was
the declared surface while nothing actually read the rows.

WHAT THIS TABLE DOES NOT CLAIM. A row is not proof that a question is unanswered. The
obvious rule — "the counterpart spoke next, so it was answered" — was measured on a live
base and closes **100%** of questions, because in a conversation every turn is followed
by the other party's. Inferring semantic closure from the episode stream would therefore
be a guess dressed as a fact. What the table carries instead is observation: a hole is
fresh (`last_seen`) and it is frequent (`seen_count`); the plan's word for the collection
is "fresh", and repetition is the strongest signal available. Ageing out is honest, where a
fabricated "closed" flag would not be.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

_GAP_MAX_AGE_DAYS = 7

#: Added after the first release of the table. `CREATE TABLE IF NOT EXISTS` cannot add
#: a column to an existing table, so both are also applied as guarded ALTERs — without
#: that, a fresh test base gets them and every live base silently does not.
_OBSERVATION_COLUMNS = (
    ("last_seen", "REAL NOT NULL DEFAULT 0"),
    ("seen_count", "INTEGER NOT NULL DEFAULT 1"),
)


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
            gap_hash TEXT NOT NULL,
            last_seen REAL NOT NULL DEFAULT 0,
            seen_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_gaps_hash ON memory_gaps(gap_hash);
        """,
    )
    conn = await cm.get(DB_NAME)
    cols = {str(r[1]) for r in await (await conn.execute("PRAGMA table_info(memory_gaps)")).fetchall()}
    for name, ddl in _OBSERVATION_COLUMNS:
        if name not in cols:
            await conn.execute(f"ALTER TABLE memory_gaps ADD COLUMN {name} {ddl}")
    # A row that predates the columns has last_seen=0, which the freshness window would
    # read as "infinitely old" and hide immediately. Its own `ts` is the honest answer:
    # the hole has been observed once, when it was written.
    await conn.execute("UPDATE memory_gaps SET last_seen = ts WHERE last_seen = 0")
    await conn.commit()


def _gap_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.strip().lower().encode("utf-8", "ignore")).hexdigest()


async def build_registry(layer: str = "user", limit: int = 20) -> dict[str, Any]:
    """Collect fresh question episodes + zero-result tails into the registry. Best-effort.

    Idempotent by gap_hash: a repeated nightly run writes no duplicates — it counts them
    (`bumped`) and refreshes `last_seen`, which is what lets `open_gaps` rank a hole that
    keeps coming back above one seen once.
    """
    cm = connection_manager
    await _ensure_table(cm)
    conn = await cm.get(DB_NAME)
    written = 0
    bumped = 0
    cutoff = time.time() - _GAP_MAX_AGE_DAYS * 86400

    async def _insert_gap(user_id: str, text: str, origin: str) -> None:
        """Idempotent insert (check-then-write; rowcount/total_changes are unreliable).

        A repeat is not a no-op: it bumps `seen_count` and refreshes `last_seen`. The same
        hole observed on three nights is the one worth pursuing, and that ranking is only
        possible if the repeat is recorded instead of discarded.
        """
        h = _gap_hash(text)
        now = time.time()
        dup = await (await conn.execute("SELECT id FROM memory_gaps WHERE gap_hash=?", (h,))).fetchone()
        if dup:
            await conn.execute(
                "UPDATE memory_gaps SET seen_count = seen_count + 1, last_seen = ? WHERE id = ?",
                (now, int(dup["id"])),
            )
            nonlocal bumped
            bumped += 1
            return
        await conn.execute(
            "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash, last_seen, seen_count) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (now, layer, user_id, text[:300], origin, h, now),
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
    return {"gaps": [dict(r) for r in rows], "written": written, "bumped": bumped}


async def open_gaps(
    layer: str = "user",
    user_id: str | None = None,
    limit: int = 5,
    max_age_days: int = _GAP_MAX_AGE_DAYS,
) -> list[dict[str, Any]]:
    """Read the registry: fresh knowledge holes, most-observed first.

    Ordered by `seen_count DESC, last_seen DESC`, so a hole the nightly pass keeps
    re-observing outranks one seen in passing. Reads degrade to an empty list on any
    failure: the caller is a session-start path, and a brief must not die because an
    optional table is absent.

    `max_age_days` is a window over `last_seen`, not a claim that older rows were
    answered — see the module docstring for why closure is not inferable here.
    """
    conn = await connection_manager.get(DB_NAME)
    cutoff = time.time() - max_age_days * 86400
    sql = "SELECT id, ts, layer, user_id, gap, origin, last_seen, seen_count FROM memory_gaps WHERE layer=? AND last_seen >= ?"
    params: list[Any] = [layer, cutoff]
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY seen_count DESC, last_seen DESC LIMIT ?"
    params.append(limit)
    try:
        rows = await (await conn.execute(sql, tuple(params))).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


async def count_open_gaps(
    layer: str = "user",
    user_id: str | None = None,
    max_age_days: int = _GAP_MAX_AGE_DAYS,
) -> int:
    """How many fresh holes there are — `open_gaps` is capped, so the count needs its own."""
    conn = await connection_manager.get(DB_NAME)
    cutoff = time.time() - max_age_days * 86400
    sql = "SELECT COUNT(*) FROM memory_gaps WHERE layer=? AND last_seen >= ?"
    params: list[Any] = [layer, cutoff]
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    try:
        row = await (await conn.execute(sql, tuple(params))).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0) if row else 0
