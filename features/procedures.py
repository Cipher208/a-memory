"""D2.5 procedural memory minimal core — HOW-knowledge with execution stats.

One table, computed success_rate (never stored — no drift), optional
"learned" notes appended on use (the trimmed §13.2 learn hook). Procedures
are a declarative cheat-sheet for the agent: it reads get() and acts; there
is no execute-engine (that is §13's full design, out of scope).

Discoverability rides the D1.3 steering route "procedural / skill knowledge"
(edited to cover both homes: memory_procedure for repeatable procedures,
wiki for rich skill docs).

Table is created lazily on first use (counterfactual.py pattern); rows ride
the global connection_manager.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any

from shared.connection import connection_manager

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _db_path() -> Any:
    return connection_manager.base_dir / "memory.db"


def _ensure_table() -> None:
    with sqlite3.connect(str(_db_path())) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS procedural_memory (
                proc_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                layer TEXT NOT NULL,
                name TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                times_used INTEGER NOT NULL DEFAULT 0,
                times_succeeded INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_id, layer, name)
            )"""
        )
        conn.commit()


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid procedure name: {name!r} (need [a-z0-9][a-z0-9_-]{{0,31}})")
    return name


def _rate(used: int, ok: int) -> float:
    return round(ok / used, 3) if used else 0.0


def _fetch(user_id: str, layer: str, name: str) -> dict[str, Any] | None:
    with sqlite3.connect(str(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM procedural_memory WHERE user_id=? AND layer=? AND name=?", (user_id, layer, name)).fetchone()
        return dict(row) if row else None


def proc_save(user_id: str, layer: str, name: str, steps: list[str], notes: str = "") -> dict[str, Any]:
    _validate_name(name)
    clean = [s for s in (str(s).strip() for s in steps or []) if s]
    if not clean:
        raise ValueError("steps must be a non-empty list of non-empty strings")
    _ensure_table()
    if _fetch(user_id, layer, name):
        raise ValueError(f"procedure already exists: {name!r}")
    now = time.time()
    with sqlite3.connect(str(_db_path())) as conn:
        conn.execute(
            "INSERT INTO procedural_memory (user_id, layer, name, steps_json, notes, times_used, times_succeeded, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)",
            (user_id, layer, name, json.dumps(clean, ensure_ascii=False), notes or "", now, now),
        )
        conn.commit()
    return {"name": name, "steps": len(clean)}


def proc_list(user_id: str, layer: str) -> list[dict[str, Any]]:
    _ensure_table()
    with sqlite3.connect(str(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT name, notes, times_used, times_succeeded, updated_at FROM procedural_memory
               WHERE user_id=? AND layer=? ORDER BY updated_at DESC""",
            (user_id, layer),
        ).fetchall()
        return [{**dict(r), "success_rate": _rate(int(r["times_used"]), int(r["times_succeeded"]))} for r in rows]


def proc_get(user_id: str, layer: str, name: str) -> dict[str, Any]:
    _validate_name(name)
    _ensure_table()
    row = _fetch(user_id, layer, name)
    if not row:
        raise ValueError(f"procedure not found: {name!r}")
    return {
        "name": row["name"],
        "steps": json.loads(row["steps_json"]),
        "notes": row["notes"],
        "times_used": int(row["times_used"]),
        "times_succeeded": int(row["times_succeeded"]),
        "success_rate": _rate(int(row["times_used"]), int(row["times_succeeded"])),
        "updated_at": float(row["updated_at"]),
    }


def proc_use(user_id: str, layer: str, name: str, success: bool, learned: str = "") -> dict[str, Any]:
    _validate_name(name)
    _ensure_table()
    row = _fetch(user_id, layer, name)
    if not row:
        raise ValueError(f"procedure not found: {name!r}")
    notes = row["notes"]
    if learned:
        notes = f"{notes}; learned: {learned}" if notes else f"learned: {learned}"
    with sqlite3.connect(str(_db_path())) as conn:
        conn.execute(
            """UPDATE procedural_memory
               SET times_used = times_used + 1,
                   times_succeeded = times_succeeded + ?,
                   notes = ?,
                   updated_at = ?
               WHERE user_id=? AND layer=? AND name=?""",
            (1 if success else 0, notes, time.time(), user_id, layer, name),
        )
        conn.commit()
    used = int(row["times_used"]) + 1
    ok = int(row["times_succeeded"]) + (1 if success else 0)
    return {"name": name, "times_used": used, "times_succeeded": ok, "success_rate": _rate(used, ok)}


def proc_delete(user_id: str, layer: str, name: str) -> dict[str, Any]:
    _validate_name(name)
    _ensure_table()
    with sqlite3.connect(str(_db_path())) as conn:
        cur = conn.execute("DELETE FROM procedural_memory WHERE user_id=? AND layer=? AND name=?", (user_id, layer, name))
        conn.commit()
        return {"name": name, "deleted": cur.rowcount > 0}
