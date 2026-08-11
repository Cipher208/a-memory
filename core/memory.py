from __future__ import annotations

"""
L4 CoreMemory — async key-value facts with importance and typed memory (B7)
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


@dataclass
class CoreEntry:
    entry_id: int
    user_id: str
    key: str
    value: str
    importance: float
    memory_kind: str
    created_at: float
    updated_at: float


class CoreMemory:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def _init_db(self) -> None:
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS core_memory (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                importance REAL DEFAULT 0.5, memory_kind TEXT, expires_at REAL,
                source TEXT DEFAULT 'manual', metadata TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_core_user ON core_memory(user_id);
            CREATE INDEX IF NOT EXISTS idx_core_key ON core_memory(key);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_core_user_key ON core_memory(user_id, key);
            CREATE INDEX IF NOT EXISTS idx_core_created ON core_memory(created_at);
            CREATE INDEX IF NOT EXISTS idx_core_updated ON core_memory(updated_at);
            CREATE INDEX IF NOT EXISTS idx_core_memory_kind ON core_memory(user_id, memory_kind);
        """,
        )

    async def save(
        self,
        user_id: str,
        key: str,
        value: str,
        importance: float | None = None,
        memory_kind: str | None = None,
        expires_at: float | None = None,
        source: str = "manual",
        metadata: dict[str, Any] | None = None,
    ) -> int:

        now = time.time()
        memory_kind, importance, expires_at = self._prepare_save_params(value, memory_kind, importance, expires_at, now)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        conn = await self._cm.get(DB_NAME)
        existing_id = await self._find_existing_id(conn, user_id, key)

        if existing_id is not None:
            await self._update_entry(conn, existing_id, value, importance, memory_kind, expires_at, source, metadata_json, now)
            entry_id = existing_id
        else:
            entry_id = await self._insert_entry(conn, user_id, key, value, importance, memory_kind, expires_at, source, metadata_json, now)

        await conn.commit()
        return entry_id

    def _prepare_save_params(self, value: str, kind_str: str | None, imp: float | None, exp: float | None, now: float) -> tuple[str, float, float | None]:
        from shared.memory_types import (
            MemoryKind,
            default_importance,
            get_policy,
            kind_for_text,
            validate_kind,
        )
        if kind_str is None:
            kind_str = kind_for_text(value).value
        if not validate_kind(kind_str):
            raise ValueError(f"invalid memory_kind: {kind_str!r}")

        kind = MemoryKind(kind_str)
        if imp is None:
            imp = default_importance(kind)
        imp = max(0.0, min(1.0, float(imp)))

        p = get_policy(kind)
        if p.requires_expires_at and exp is None:
            exp = now + 30 * 86400

        return kind_str, imp, exp

    async def _find_existing_id(self, conn: Any, user_id: str, key: str) -> int | None:
        cursor = await conn.execute("SELECT entry_id FROM core_memory WHERE user_id=? AND key=?", (user_id, key))
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    async def _update_entry(self, conn: Any, eid: int, val: str, imp: float, kind: str, exp: float | None, src: str, meta: str, now: float) -> None:
        await conn.execute(
            """UPDATE core_memory SET value=?, importance=?, memory_kind=?,
               expires_at=?, source=?, metadata=?, updated_at=?
               WHERE entry_id=?""",
            (val, imp, kind, exp, src, meta, now, eid),
        )

    async def _insert_entry(self, conn: Any, uid: str, key: str, val: str, imp: float, kind: str, exp: float | None, src: str, meta: str, now: float) -> int:
        cursor = await conn.execute(
            """INSERT INTO core_memory
               (user_id, key, value, importance, memory_kind, expires_at,
                source, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, key, val, imp, kind, exp, src, meta, now, now),
        )
        return int(cursor.lastrowid or 0)

    async def get(self, user_id: str, key: str) -> CoreEntry | None:
        """Get a fact by key. Returns None if not found."""
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("SELECT * FROM core_memory WHERE user_id=? AND key=?", (user_id, key))
        row = await cursor.fetchone()
        return self._row_to_entry(row) if row else None

    async def get_or_default(self, user_id: str, key: str, default: str = "") -> str:
        """Get value or return default (never returns None)."""
        entry = await self.get(user_id, key)
        return entry.value if entry else default

    async def get_all(self, user_id: str, limit: int = 50) -> list[CoreEntry]:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("SELECT * FROM core_memory WHERE user_id=? ORDER BY importance DESC LIMIT ?", (user_id, limit))
        rows = await cursor.fetchall()
        return [self._row_to_entry(r) for r in rows]

    async def delete(self, user_id: str, key: str) -> bool:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("DELETE FROM core_memory WHERE user_id=? AND key=?", (user_id, key))
        await conn.commit()
        return cursor.rowcount > 0

    async def search(self, user_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT * FROM core_memory WHERE user_id=? AND (key LIKE ? OR value LIKE ?) ORDER BY importance DESC LIMIT ?",
            (user_id, f"%{query}%", f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [{"key": str(r["key"]), "value": str(r["value"]), "importance": float(r["importance"])} for r in rows]

    async def count(self, user_id: str | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if user_id:
            cursor = await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id=?", (user_id,))
        else:
            cursor = await conn.execute("SELECT COUNT(*) FROM core_memory")
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _row_to_entry(self, row: dict[str, Any] | Any) -> CoreEntry:
        return CoreEntry(
            entry_id=int(row["entry_id"]),
            user_id=str(row["user_id"]),
            key=str(row["key"]),
            value=str(row["value"]),
            importance=float(row["importance"]),
            memory_kind=str(row["memory_kind"] or "fact"),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def list_by_kind(
        self,
        user_id: str,
        memory_kind: str,
        min_importance: float = 0.0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List memories filtered by type."""
        conn = await self._cm.get(DB_NAME)
        rows = await (
            await conn.execute(
                """SELECT key, value, importance, memory_kind, expires_at,
                      created_at, updated_at
               FROM core_memory
               WHERE user_id=? AND memory_kind=? AND importance >= ?
               ORDER BY importance DESC, updated_at DESC
               LIMIT ?""",
                (user_id, memory_kind, min_importance, limit),
            )
        ).fetchall()
        return [dict(r) for r in rows]
