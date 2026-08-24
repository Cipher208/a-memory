from __future__ import annotations

"""
DreamBuffer — async staging memories with TTL
"""

import json
import time
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


class DreamBuffer:
    def __init__(self, cm: AsyncConnectionManager | None = None, layer: str = "user"):
        self._cm = cm or connection_manager
        self.layer = layer
        self._ready = False

    async def ensure(self) -> None:
        """Idempotent schema check: create fresh, or migrate pre-layer tables.

        PRAGMA-first instead of blanket-try/ALTER — a suppressed 'database is
        locked' here silently dropped the column and corrupted later writes.
        """
        if self._ready:
            return
        conn = await self._cm.get(DB_NAME)
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(staging_memories)")).fetchall()]
        if not cols:
            await self._init_db()
            self._ready = True
            return
        if "layer" not in cols:
            await conn.execute("ALTER TABLE staging_memories ADD COLUMN layer TEXT NOT NULL DEFAULT 'user'")
            await conn.commit()
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_staging_layer_user ON staging_memories(layer, user_id)")
        await conn.commit()
        self._ready = True

    async def _init_db(self) -> None:
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS staging_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL DEFAULT 'user',
                user_id TEXT NOT NULL DEFAULT 'default',
                session_id TEXT NOT NULL, event_id TEXT,
                content TEXT NOT NULL, importance REAL DEFAULT 0.5,
                metadata TEXT DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_staging_layer_user ON staging_memories(layer, user_id);
        """,
        )

    async def add(
        self,
        user_id: str,
        session_id: str,
        content: str,
        importance: float = 0.5,
        event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO staging_memories (layer, user_id, session_id, event_id, content, importance, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.layer, user_id, session_id, event_id, content, importance, json.dumps(metadata or {})),
        )
        await conn.commit()
        last_id: Any = cursor.lastrowid
        return int(last_id) if last_id is not None else 0

    async def get_staging(self, user_id: str = "default", session_id: str | None = None) -> list[dict[str, Any]]:
        conn = await self._cm.get(DB_NAME)
        await self.ensure()
        if session_id:
            cursor = await conn.execute(
                "SELECT * FROM staging_memories WHERE layer=? AND user_id=? AND session_id=? ORDER BY created_at",
                (self.layer, user_id, session_id),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM staging_memories WHERE layer=? AND user_id=? ORDER BY created_at",
                (self.layer, user_id),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "content": r["content"],
                "importance": r["importance"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            }
            for r in rows
        ]

    async def clear_staging(self, user_id: str = "default", session_id: str | None = None) -> int:
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        if session_id:
            cursor = await conn.execute("DELETE FROM staging_memories WHERE user_id=? AND session_id=?", (user_id, session_id))
        else:
            cursor = await conn.execute("DELETE FROM staging_memories WHERE user_id=?", (user_id,))
        await conn.commit()
        return int(cursor.rowcount)

    async def cleanup_old(self, max_age_hours: int = 24, max_count: int = 500) -> dict[str, int]:
        now = time.time()
        conn = await self._cm.get(DB_NAME)
        result = {"by_age": 0, "by_count": 0}
        cutoff = now - (max_age_hours * 3600)
        cursor = await conn.execute(
            "DELETE FROM staging_memories WHERE created_at < datetime(?, 'unixepoch')",
            (cutoff,),
        )
        result["by_age"] = int(cursor.rowcount)

        rows = await (
            await conn.execute(
                "SELECT user_id, COUNT(*) as cnt FROM staging_memories GROUP BY user_id HAVING cnt > ?",
                (max_count,),
            )
        ).fetchall()
        for row in rows:
            excess = int(row["cnt"]) - max_count
            cursor = await conn.execute(
                "DELETE FROM staging_memories WHERE id IN (SELECT id FROM staging_memories WHERE user_id=? ORDER BY created_at ASC LIMIT ?)",
                (row["user_id"], excess),
            )
            result["by_count"] += int(cursor.rowcount)
        await conn.commit()
        return result

    async def count(self, user_id: str = "default") -> int:
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM staging_memories WHERE user_id=?", (user_id,))).fetchone()
        return int(row[0]) if row else 0

    async def count_all(self) -> int:
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM staging_memories")).fetchone()
        return int(row[0]) if row else 0
