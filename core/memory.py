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
        from shared.memory_types import (
            MemoryKind,
            default_importance,
            get_policy,
            kind_for_text,
            validate_kind,
        )

        now = time.time()

        # Auto-classification if kind not specified
        if memory_kind is None:
            memory_kind = kind_for_text(value).value
        if not validate_kind(memory_kind):
            raise ValueError(f"invalid memory_kind: {memory_kind!r}")
        kind = MemoryKind(memory_kind)

        # Auto-importance from type policy
        if importance is None:
            importance = default_importance(kind)
        importance = max(0.0, min(1.0, float(importance)))

        # Auto expires_at for types that require it
        p = get_policy(kind)
        if p.requires_expires_at and expires_at is None:
            logger.warning("memory_kind=%s requires expires_at; auto-set +30d", kind.value)
            expires_at = now + 30 * 86400

        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT entry_id FROM core_memory WHERE user_id=? AND key=?",
            (user_id, key),
        )
        existing = await cursor.fetchone()

        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

        if existing:
            await conn.execute(
                """UPDATE core_memory SET value=?, importance=?, memory_kind=?,
                   expires_at=?, source=?, metadata=?, updated_at=?
                   WHERE entry_id=?""",
                (value, importance, memory_kind, expires_at, source, metadata_json, now, int(existing["entry_id"])),
            )
            entry_id = int(existing["entry_id"])
        else:
            cursor = await conn.execute(
                """INSERT INTO core_memory
                   (user_id, key, value, importance, memory_kind, expires_at,
                    source, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, key, value, importance, memory_kind, expires_at, source, metadata_json, now, now),
            )
            entry_id = int(cursor.lastrowid or 0)
        await conn.commit()
        return entry_id

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
