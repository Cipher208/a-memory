"""
ArchivedMemories — async archived memory storage
"""

from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


class ArchivedMemories:
    def __init__(self, cm: "AsyncConnectionManager" | None = None):
        self._cm = cm or connection_manager

    async def _init_db(self):
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS archived_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                original_id INTEGER, content TEXT NOT NULL,
                memory_type TEXT, importance REAL,
                archive_reason TEXT NOT NULL,
                archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """,
        )

    async def archive(
        self,
        user_id: str,
        content: str,
        memory_type: str | None = None,
        importance: float | None = None,
        original_id: int | None = None,
        reason: str = "manual",
    ) -> int:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, original_id, content, memory_type, importance, reason),
        )
        await conn.commit()
        return cursor.lastrowid

    async def get_archived(self, user_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT * FROM archived_memories WHERE user_id=? ORDER BY archived_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "content": r["content"],
                "importance": r["importance"],
                "archive_reason": r["archive_reason"],
                "archived_at": r["archived_at"],
            }
            for r in rows
        ]

    async def count(self, user_id: str = "default") -> int:
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM archived_memories WHERE user_id=?", (user_id,))).fetchone()
        return row[0] if row else 0

    async def restore(self, archived_id: int) -> dict[str, Any] | None:
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT * FROM archived_memories WHERE id=?", (archived_id,))).fetchone()
        if row:
            await conn.execute("DELETE FROM archived_memories WHERE id=?", (archived_id,))
            await conn.commit()
            return {"content": row["content"], "importance": row["importance"]}
        return None
