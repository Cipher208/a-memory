"""D1.12 memory stash — git-stash for the working context (L1 + scratchpad).

After layer isolation (MemoryManager keys per user) the surviving use case is
ONE agent switching between contexts (project A ↔ project B): stash the
current working set, work elsewhere, pop it back. L1 entries are stored with
role/content/tokens (timestamps refreshed on pop — resuming work makes the
chatter "recent" again). L4/L2 are NOT stashed: identity is handled by D1.11
branches and D1.14 snapshots.

Pop refuses on a non-empty current scratchpad (stash it first) — no silent
data loss. L1 is replaced silently (ephemeral chatter).

Table is created lazily on first use (counterfactual.py pattern); rows ride
the global connection_manager like scratchpad.py.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from features.scratchpad import _db_path, clear_entries, read_entries, write_entry

if TYPE_CHECKING:
    from core.reflex import ReflexBuffer

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _ensure_table() -> None:
    with sqlite3.connect(str(_db_path())) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_stash (
                stash_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                layer TEXT NOT NULL,
                name TEXT NOT NULL,
                l1_json TEXT NOT NULL,
                scratchpad_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        conn.commit()


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid stash name: {name!r} (need [a-z0-9][a-z0-9_-]{{0,31}})")
    return name


def _fetch_row(user_id: str, layer: str, name: str) -> dict[str, Any] | None:
    with sqlite3.connect(str(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM memory_stash WHERE user_id=? AND layer=? AND name=?", (user_id, layer, name)
        ).fetchone()
        return dict(row) if row else None


async def stash_save(mem: Any, user_id: str, layer: str, name: str) -> dict[str, Any]:
    _validate_name(name)
    _ensure_table()
    if _fetch_row(user_id, layer, name):
        raise ValueError(f"stash already exists: {name!r}")

    l1_items = [{"role": e.role, "content": e.content, "tokens": int(e.tokens or 0)} for e in mem.l1.get_full()]
    pad_items = [{"key": e["key"], "content": e["content"]} for e in read_entries(user_id, layer)]
    if not l1_items and not pad_items:
        raise ValueError("nothing to stash: L1 and scratchpad are empty")

    with sqlite3.connect(str(_db_path())) as conn:
        conn.execute(
            "INSERT INTO memory_stash (user_id, layer, name, l1_json, scratchpad_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, layer, name, json.dumps(l1_items, ensure_ascii=False), json.dumps(pad_items, ensure_ascii=False), time.time()),
        )
        conn.commit()

    mem.l1.clear()
    if pad_items:
        clear_entries(user_id, layer)
    return {"name": name, "l1_items": len(l1_items), "scratchpad_items": len(pad_items)}


def stash_list(user_id: str, layer: str) -> list[dict[str, Any]]:
    _ensure_table()
    with sqlite3.connect(str(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT stash_id, user_id, layer, name, created_at,
                      (SELECT COUNT(*) FROM json_each(memory_stash.l1_json)) AS l1_items,
                      (SELECT COUNT(*) FROM json_each(memory_stash.scratchpad_json)) AS scratchpad_items
               FROM memory_stash WHERE user_id=? AND layer=? ORDER BY created_at DESC""",
            (user_id, layer),
        ).fetchall()
        return [dict(r) for r in rows]


async def stash_pop(mem: Any, user_id: str, layer: str, name: str) -> dict[str, Any]:
    _validate_name(name)
    _ensure_table()
    if read_entries(user_id, layer):
        raise ValueError("current scratchpad is not empty — stash the current context first")
    row = _fetch_row(user_id, layer, name)
    if not row:
        raise ValueError(f"stash not found: {name!r}")

    l1_items = json.loads(row["l1_json"])
    pad_items = json.loads(row["scratchpad_json"])

    mem.l1.clear()
    for item in l1_items:
        mem.l1.add(item["role"], item["content"], int(item.get("tokens") or 0))
    for item in pad_items:
        await write_entry(user_id, layer, item["key"], item["content"])

    stash_drop(user_id, layer, name)
    return {"name": name, "l1_items": len(l1_items), "scratchpad_items": len(pad_items)}


def stash_drop(user_id: str, layer: str, name: str) -> dict[str, Any]:
    _validate_name(name)
    _ensure_table()
    with sqlite3.connect(str(_db_path())) as conn:
        cur = conn.execute("DELETE FROM memory_stash WHERE user_id=? AND layer=? AND name=?", (user_id, layer, name))
        conn.commit()
        return {"name": name, "dropped": cur.rowcount > 0}
