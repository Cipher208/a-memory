"""
L3 EpisodicMemory — async important moments with emotional weight
"""

import json
import time
from dataclasses import dataclass

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


@dataclass
class Episode:
    episode_id: int
    user_id: str
    summary: str
    emotional_weight: float
    tags: list[str]
    created_at: float


class EpisodicMemory:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def _init_db(self):
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                emotional_weight REAL DEFAULT 0.5,
                tags TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes(user_id);
            CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(created_at);
        """,
        )

    async def save(self, user_id: str, summary: str, emotional_weight: float = 0.5, tags: list[str] | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO episodes (user_id, summary, emotional_weight, tags, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, summary, emotional_weight, json.dumps(tags or []), time.time()),
        )
        await conn.commit()
        return cursor.lastrowid

    async def get_episodes(self, user_id: str, limit: int = 20, offset: int = 0) -> list[Episode]:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT * FROM episodes WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [self._row_to_episode(r) for r in rows]

    async def search_by_tag(self, user_id: str, tag: str, limit: int = 10) -> list[Episode]:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT * FROM episodes WHERE user_id=? AND tags LIKE ? ORDER BY created_at DESC LIMIT ?",
            (user_id, f'%"{tag}"%', limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_episode(r) for r in rows]

    async def search(self, user_id: str, query: str, limit: int = 10) -> list:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT * FROM episodes WHERE user_id=? AND summary LIKE ? ORDER BY created_at DESC LIMIT ?",
            (user_id, f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_episode(r) for r in rows]

    async def archive_old(self, user_id: str, days: int = 90) -> int:
        """Archive old episodes into ArchivedMemories, then delete them."""
        conn = await self._cm.get(DB_NAME)
        cutoff = time.time() - (days * 86400)
        cursor = await conn.execute(
            "SELECT * FROM episodes WHERE user_id=? AND created_at < ? AND emotional_weight < 0.3",
            (user_id, cutoff),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0

        from shared.archived_memories import ArchivedMemories

        am = ArchivedMemories()
        archived_count = 0
        for row in rows:
            await am.archive(
                user_id=user_id,
                content=row["summary"],
                memory_type="episode",
                importance=row["emotional_weight"],
                original_id=row["episode_id"],
                reason=f"inactive_{days}d",
            )
            archived_count += 1

        ids = [row["episode_id"] for row in rows]
        if not ids:
            return archived_count

        placeholders = ",".join(["?"] * len(ids))
        sql = f"DELETE FROM episodes WHERE episode_id IN ({placeholders})"
        await conn.execute(sql, ids)
        await conn.commit()
        return archived_count

    async def count(self, user_id: str) -> int:
        """Count episodes for a user (fast COUNT query)."""
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT COUNT(*) as cnt FROM episodes WHERE user_id=?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    def _row_to_episode(self, row) -> Episode:
        return Episode(
            episode_id=row["episode_id"],
            user_id=row["user_id"],
            summary=row["summary"],
            emotional_weight=row["emotional_weight"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            created_at=row["created_at"],
        )
