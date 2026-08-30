from __future__ import annotations

"""
L3 EpisodicMemory — async important moments with emotional weight
"""

import json
import time
from dataclasses import dataclass
from typing import Any

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
    def __init__(self, cm: AsyncConnectionManager | None = None, layer: str = "user"):
        self._cm = cm or connection_manager
        self.layer = layer

    async def _init_db(self) -> None:
        await self._cm.execute_script(
            DB_NAME,
            f"""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL DEFAULT '{self.layer}',
                user_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                emotional_weight REAL DEFAULT 0.5,
                tags TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes(user_id);
            CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(created_at);
            CREATE INDEX IF NOT EXISTS idx_episodes_layer ON episodes(layer, user_id);
            CREATE INDEX IF NOT EXISTS idx_episodes_layer_time ON episodes(layer, user_id, created_at DESC);
        """,
        )

    async def save(self, user_id: str, summary: str, emotional_weight: float = 0.5, tags: list[str] | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (self.layer, user_id, summary, emotional_weight, json.dumps(tags or []), time.time()),
        )
        await conn.commit()
        return int(cursor.lastrowid or 0)

    async def get_episodes(self, user_id: str, limit: int = 20, offset: int = 0) -> list[Episode]:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT * FROM episodes WHERE layer=? AND user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (self.layer, user_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [self._row_to_episode(r) for r in rows]

    async def search_by_tag(self, user_id: str, tag: str, limit: int = 10) -> list[Episode]:
        """B7: Specialized tag search (part of search() logic, but kept for API compat)."""
        return await self.search(user_id, tag, limit, use_tag_match=True)

    async def get_by_id(self, episode_id: int) -> Episode | None:
        """Fetch one episode by id (D2.2 promotion pipeline)."""
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,))
        row = await cursor.fetchone()
        return self._row_to_episode(row) if row else None

    async def add_tag(self, episode_id: int, tag: str) -> bool:
        """Append a tag to an episode's JSON tag list (idempotent). D2.2."""
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("SELECT tags FROM episodes WHERE episode_id = ?", (episode_id,))
        row = await cursor.fetchone()
        if row is None:
            return False
        try:
            tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []
        if tag in tags:
            return True
        tags.append(tag)
        await conn.execute("UPDATE episodes SET tags = ? WHERE episode_id = ?", (json.dumps(tags), episode_id))
        await conn.commit()
        return True

    async def search(self, user_id: str, query: str, limit: int = 10, use_tag_match: bool = False) -> list[Episode]:
        """Unified search across summary and tags."""
        conn = await self._cm.get(DB_NAME)
        params: tuple[Any, ...]
        if use_tag_match:
            sql = "SELECT * FROM episodes WHERE layer=? AND user_id=? AND tags LIKE ? ORDER BY created_at DESC LIMIT ?"
            params = (self.layer, user_id, f'%"{query}"%', limit)
        else:
            # Tokenized summary match: any word hits, newest-first ordering kept.
            tokens = [w for w in query.split() if w]
            if not tokens:
                return []
            like_conds = " OR ".join(["summary LIKE ?" for _ in tokens])
            sql = f"SELECT * FROM episodes WHERE layer=? AND user_id=? AND ({like_conds}) ORDER BY created_at DESC LIMIT ?"
            params = (self.layer, user_id, *[f"%{w}%" for w in tokens], limit)

        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_episode(r) for r in rows]

    async def archive_old(self, user_id: str, days: int = 90) -> int:
        """Archive old episodes into ArchivedMemories, then delete them."""
        conn = await self._cm.get(DB_NAME)
        cutoff = time.time() - (days * 86400)
        cursor = await conn.execute(
            "SELECT * FROM episodes WHERE layer=? AND user_id=? AND created_at < ? AND emotional_weight < 0.3",
            (self.layer, user_id, cutoff),
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
                content=str(row["summary"]),
                memory_type="episode",
                importance=float(row["emotional_weight"]),
                original_id=int(row["episode_id"]),
                reason=f"inactive_{days}d",
            )
            archived_count += 1

        ids = [int(row["episode_id"]) for row in rows]
        if not ids:
            return archived_count

        placeholders = ",".join(["?"] * len(ids))
        sql = f"DELETE FROM episodes WHERE episode_id IN ({placeholders})"
        await conn.execute(sql, tuple(ids))
        await conn.commit()
        return archived_count

    async def count(self, user_id: str) -> int:
        """Count episodes for a user (fast COUNT query)."""
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT COUNT(*) as cnt FROM episodes WHERE layer=? AND user_id=?",
            (self.layer, user_id),
        )
        row = await cursor.fetchone()
        return int(row["cnt"]) if row and row[0] is not None else 0

    async def delete_older_than(self, user_id: str, cutoff: float) -> int:
        """Delete this layer's episodes with created_at > cutoff (recent purge)."""
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "DELETE FROM episodes WHERE layer=? AND user_id=? AND created_at > ?",
            (self.layer, user_id, cutoff),
        )
        await conn.commit()
        return int(cursor.rowcount)

    async def delete_by_ids(self, episode_ids: list[int]) -> int:
        """Delete episodes by id. Returns the number of rows removed."""
        if not episode_ids:
            return 0
        conn = await self._cm.get(DB_NAME)
        placeholders = ",".join(["?"] * len(episode_ids))
        cur = await conn.execute(f"DELETE FROM episodes WHERE episode_id IN ({placeholders})", tuple(episode_ids))
        await conn.commit()
        return int(cur.rowcount)

    def _row_to_episode(self, row: dict[str, Any] | Any) -> Episode:
        return Episode(
            episode_id=int(row["episode_id"]),
            user_id=str(row["user_id"]),
            summary=str(row["summary"]),
            emotional_weight=float(row["emotional_weight"]),
            tags=list(json.loads(row["tags"])) if row["tags"] else [],
            created_at=float(row["created_at"]),
        )
