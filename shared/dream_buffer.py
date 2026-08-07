"""
DreamBuffer — async staging memories with TTL
"""

import json
import time
from typing import Any, Optional

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


class DreamBuffer:
    def __init__(self, cm: Optional["AsyncConnectionManager"] = None):
        self._cm = cm or connection_manager

    async def _init_db(self):
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS staging_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                session_id TEXT NOT NULL, event_id TEXT,
                content TEXT NOT NULL, importance REAL DEFAULT 0.5,
                metadata TEXT DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """,
        )

    async def add(
        self,
        user_id: str,
        session_id: str,
        content: str,
        importance: float = 0.5,
        event_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO staging_memories (user_id, session_id, event_id, content, importance, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, session_id, event_id, content, importance, json.dumps(metadata or {})),
        )
        await conn.commit()
        return cursor.lastrowid

    async def get_staging(self, user_id: str = "default", session_id: Optional[str] = None) -> list[dict[str, Any]]:
        conn = await self._cm.get(DB_NAME)
        if session_id:
            cursor = await conn.execute(
                "SELECT * FROM staging_memories WHERE user_id=? AND session_id=? ORDER BY created_at",
                (user_id, session_id),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM staging_memories WHERE user_id=? ORDER BY created_at",
                (user_id,),
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

    async def clear_staging(self, user_id: str = "default", session_id: Optional[str] = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if session_id:
            cursor = await conn.execute("DELETE FROM staging_memories WHERE user_id=? AND session_id=?", (user_id, session_id))
        else:
            cursor = await conn.execute("DELETE FROM staging_memories WHERE user_id=?", (user_id,))
        await conn.commit()
        return cursor.rowcount

    async def cleanup_old(self, max_age_hours: int = 24, max_count: int = 500) -> dict[str, int]:
        now = time.time()
        conn = await self._cm.get(DB_NAME)
        result = {"by_age": 0, "by_count": 0}
        cutoff = now - (max_age_hours * 3600)
        cursor = await conn.execute(
            "DELETE FROM staging_memories WHERE created_at < datetime(?, 'unixepoch')",
            (cutoff,),
        )
        result["by_age"] = cursor.rowcount

        rows = await (
            await conn.execute(
                "SELECT user_id, COUNT(*) as cnt FROM staging_memories GROUP BY user_id HAVING cnt > ?",
                (max_count,),
            )
        ).fetchall()
        for row in rows:
            excess = row["cnt"] - max_count
            cursor = await conn.execute(
                "DELETE FROM staging_memories WHERE id IN (SELECT id FROM staging_memories WHERE user_id=? ORDER BY created_at ASC LIMIT ?)",
                (row["user_id"], excess),
            )
            result["by_count"] += cursor.rowcount
        await conn.commit()
        return result

    async def count(self, user_id: str = "default") -> int:
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM staging_memories WHERE user_id=?", (user_id,))).fetchone()
        return row[0] if row else 0

    async def count_all(self) -> int:
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM staging_memories")).fetchone()
        return row[0] if row else 0
