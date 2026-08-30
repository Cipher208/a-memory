"""D1.15 agent scratchpad (L2.5) — working memory between L2 (session) and L3.

The agent writes its own hypotheses/plans/drafts here; entries re-inject at
session start (scratchpad block in build_inject_blocks). Judgment of "useful
vs garbage" is the agent's call: promote_entries moves entries into L3/L4 and
drops them from the pad. Sync sqlite3 over connection_manager (rehydrate.py
pattern); missing table degrades gracefully. Cap: 20 entries per user+layer
(oldest evicted on overflow).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

MAX_ENTRIES = 20


def _db_path() -> Any:
    from shared.connection import connection_manager

    return connection_manager.base_dir / "memory.db"


def _ensure_table() -> None:
    with sqlite3.connect(str(_db_path())) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_scratchpad ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', key TEXT NOT NULL,"
            " content TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
            " UNIQUE(user_id, layer, key))"
        )
        conn.commit()


async def write_entry(user_id: str, layer: str, key: str, content: str) -> bool:
    """Upsert one scratchpad entry (refreshes updated_at); evicts over cap."""
    now = time.time()
    try:
        _ensure_table()
        with sqlite3.connect(str(_db_path())) as conn:
            conn.execute(
                "INSERT INTO agent_scratchpad (user_id, layer, key, content, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id, layer, key) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                (user_id, layer, key, content, now, now),
            )
            # cap: evict oldest beyond MAX_ENTRIES
            conn.execute(
                "DELETE FROM agent_scratchpad WHERE user_id=? AND layer=? AND id NOT IN ("
                " SELECT id FROM agent_scratchpad WHERE user_id=? AND layer=?"
                " ORDER BY updated_at DESC LIMIT ?)",
                (user_id, layer, user_id, layer, MAX_ENTRIES),
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.debug("scratchpad write failed: %s", exc)
        return False


def read_entries(user_id: str, layer: str, key: str = "") -> list[dict[str, Any]]:
    """Scratchpad entries, newest-first; one key when given."""
    try:
        _ensure_table()
        with sqlite3.connect(str(_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            if key:
                rows = conn.execute(
                    "SELECT * FROM agent_scratchpad WHERE user_id=? AND layer=? AND key=? ORDER BY updated_at DESC",
                    (user_id, layer, key),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_scratchpad WHERE user_id=? AND layer=? ORDER BY updated_at DESC",
                    (user_id, layer),
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("scratchpad read failed: %s", exc)
        return []


def clear_entries(user_id: str, layer: str, key: str = "") -> int:
    """Clear one entry (key given) or the whole pad. Returns rows removed."""
    try:
        _ensure_table()
        with sqlite3.connect(str(_db_path())) as conn:
            if key:
                cur = conn.execute(
                    "DELETE FROM agent_scratchpad WHERE user_id=? AND layer=? AND key=?",
                    (user_id, layer, key),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM agent_scratchpad WHERE user_id=? AND layer=?",
                    (user_id, layer),
                )
            conn.commit()
            return int(cur.rowcount or 0)
    except Exception as exc:
        logger.debug("scratchpad clear failed: %s", exc)
        return 0


async def promote_entries(mem: Any, user_id: str, layer: str, keys: list[str], to: str = "l3") -> dict[str, Any]:
    """Move agent-judged-useful entries into L3 (episode) or L4 (fact).

    The agent is the distiller (no LLM in ariel — D2.2 ceiling applies):
    promotion copies the entry content and drops it from the pad.
    """
    promoted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for key in keys:
        rows = read_entries(user_id, layer, key=key)
        if not rows:
            skipped.append({"key": key, "reason": "not_found"})
            continue
        entry = rows[0]
        try:
            if to == "l4":
                await mem.remember(f"scratchpad_{key}", entry["content"], 0.8)
            else:
                await mem.l3.save(user_id, entry["content"], 0.8, ["scratchpad_promoted"])
            clear_entries(user_id, layer, key=key)
            promoted.append({"key": key, "to": to})
        except Exception as exc:
            skipped.append({"key": key, "reason": f"save_failed: {exc}"})
    return {"promoted": promoted, "skipped": skipped, "count": len(promoted)}
