"""ArchivedMemories — async archived memory storage."""

from __future__ import annotations
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


class ArchivedMemories:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager
        self._ready = False

    async def ensure(self) -> None:
        """Create the table if it is not there yet, once per instance.

        Same shape as `DreamBuffer.ensure`, and for the same reason: this class
        is constructed in several places that never run migrations, so a method
        that touches the table has to be able to bring it into existence.

        Why it was needed: `archive()` and `get_archived()` used to assume the
        table existed. On a database that had run the migration it did, which
        hid the problem — `tests/test_shared/test_shared.py::test_archived_memories`
        passed only when another test had migrated the shared session database
        first. Run alone, or with `-p no:randomly`, it failed with
        `no such table: archived_memories`. A test that needs a neighbour to
        pass is not testing the class.
        """
        if self._ready:
            return
        conn = await self._cm.get(DB_NAME)
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(archived_memories)")).fetchall()]
        if not cols:
            await self._init_db()
        self._ready = True

    async def _init_db(self) -> None:
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
            CREATE INDEX IF NOT EXISTS idx_archived_user ON archived_memories(user_id, archived_at DESC);
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
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, original_id, content, memory_type, importance, reason),
        )
        await conn.commit()
        last_id: Any = cursor.lastrowid
        return int(last_id) if last_id is not None else 0

    async def get_archived(self, user_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
        await self.ensure()
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
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM archived_memories WHERE user_id=?", (user_id,))).fetchone()
        return int(row[0]) if row else 0

    async def restore(self, archived_id: int) -> dict[str, Any] | None:
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT * FROM archived_memories WHERE id=?", (archived_id,))).fetchone()
        if row:
            await conn.execute("DELETE FROM archived_memories WHERE id=?", (archived_id,))
            await conn.commit()
            return {"content": row["content"], "importance": row["importance"]}
        return None
