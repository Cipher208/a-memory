from __future__ import annotations

"""
DreamBuffer — async staging memories with TTL
"""

import json
import logging
import time
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch, get_write_dispatch

logger = logging.getLogger(__name__)


class DreamBuffer:
    def __init__(self, cm: AsyncConnectionManager | None = None, layer: str = "user"):
        self._cm = cm or connection_manager
        self.layer = layer
        self._ready = False

    @property
    def _dispatch(self) -> WriteDispatch:
        return get_write_dispatch(self._cm)

    async def ensure(self) -> None:
        """Idempotent schema check: create fresh, or migrate pre-layer tables.

        PRAGMA-first instead of blanket-try/ALTER — a suppressed 'database is
        locked' here silently dropped the column and corrupted later writes.
        """
        if self._ready:
            return
        cols = [r["name"] for r in await self._dispatch.query(DB_NAME, "PRAGMA table_info(staging_memories)")]
        if not cols:
            await self._init_db()
            self._ready = True
            return
        if "layer" not in cols:
            await self._dispatch.write(DB_NAME, "ALTER TABLE staging_memories ADD COLUMN layer TEXT NOT NULL DEFAULT 'user'")
        await self._dispatch.write(DB_NAME, "CREATE INDEX IF NOT EXISTS idx_staging_layer_user ON staging_memories(layer, user_id)")
        self._ready = True

    async def _init_db(self) -> None:
        await self._dispatch.execute_script(
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
            CREATE INDEX IF NOT EXISTS idx_staging_layer_user_session ON staging_memories(layer, user_id, session_id, created_at);
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
        return await self._dispatch.insert(
            DB_NAME,
            "INSERT INTO staging_memories (layer, user_id, session_id, event_id, content, importance, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.layer, user_id, session_id, event_id, content, importance, json.dumps(metadata or {})),
        )

    async def get_staging(self, user_id: str = "default", session_id: str | None = None) -> list[dict[str, Any]]:
        await self.ensure()
        if session_id:
            rows = await self._dispatch.query(
                DB_NAME,
                "SELECT * FROM staging_memories WHERE layer=? AND user_id=? AND session_id=? ORDER BY created_at",
                (self.layer, user_id, session_id),
            )
        else:
            rows = await self._dispatch.query(
                DB_NAME,
                "SELECT * FROM staging_memories WHERE layer=? AND user_id=? ORDER BY created_at",
                (self.layer, user_id),
            )
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
        if session_id:
            return await self._dispatch.write(DB_NAME, "DELETE FROM staging_memories WHERE user_id=? AND session_id=?", (user_id, session_id))
        return await self._dispatch.write(DB_NAME, "DELETE FROM staging_memories WHERE user_id=?", (user_id,))

    async def cleanup_old(self, max_age_hours: int = 24, max_count: int = 500) -> dict[str, int]:
        # Housekeeping must not ride, nor wait out, somebody else's transaction: a
        # foreign write txn on the shared handle means the dispatcher's own connection
        # would block for the full busy timeout and then raise. The next tick retries.
        shared = await self._cm.get(DB_NAME)
        if shared.in_transaction:
            logger.warning("staging cleanup skipped, a foreign transaction is open")
            return {"by_age": 0, "by_count": 0}

        now = time.time()
        cutoff = now - (max_age_hours * 3600)

        async def _cleanup(conn: Any) -> dict[str, int]:
            result = {"by_age": 0, "by_count": 0}
            cur = await conn.execute(
                "DELETE FROM staging_memories WHERE created_at < datetime(?, 'unixepoch')",
                (cutoff,),
            )
            result["by_age"] = int(cur.rowcount)
            rows = await (
                await conn.execute(
                    "SELECT user_id, COUNT(*) as cnt FROM staging_memories GROUP BY user_id HAVING cnt > ?",
                    (max_count,),
                )
            ).fetchall()
            for row in rows:
                excess = int(row["cnt"]) - max_count
                cur = await conn.execute(
                    "DELETE FROM staging_memories WHERE id IN (SELECT id FROM staging_memories WHERE user_id=? ORDER BY created_at ASC LIMIT ?)",
                    (row["user_id"], excess),
                )
                result["by_count"] += int(cur.rowcount)
            return result

        return await self._dispatch.atomic(DB_NAME, _cleanup)

    async def count(self, user_id: str = "default") -> int:
        total = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM staging_memories WHERE user_id=?", (user_id,))
        return int(total) if total is not None else 0

    async def count_all(self) -> int:
        total = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM staging_memories")
        return int(total) if total is not None else 0
