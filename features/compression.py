from __future__ import annotations

"""
MemoryCompressor — async dedup and compression
"""

import logging
import time

from typing import cast
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


class MemoryCompressor:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def deduplicate_core(self, user_id: str) -> int:
        """Legacy-safe same-key dedup per (layer, user_id).

        Current schemas enforce UNIQUE(layer, user_id, key), so on live DBs
        this removes 0 rows; it only cleans databases created before that
        index existed. Partitioning BY layer is essential: a user fact and
        an agent fact sharing a key are legitimate distinct memories.
        """
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            """DELETE FROM core_memory WHERE entry_id IN (
                   SELECT entry_id FROM (
                       SELECT entry_id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY layer, user_id, key
                                  ORDER BY updated_at DESC, entry_id DESC
                              ) AS rn
                       FROM core_memory
                       WHERE user_id=?
                   )
                   WHERE rn > 1
               )""",
            (user_id,),
        )
        removed = int(cursor.rowcount or 0)
        await conn.commit()
        return removed

    async def compress_episodes(self, user_id: str, min_weight: float = 0.3, layer: str = "user") -> int:
        """Drop old low-weight episodes of ONE layer, archiving them first.

        Layer-scoped deliberately: agent-layer episodes are identity data
        and must never be swept by user-memory maintenance.
        """
        conn = await self._cm.get(DB_NAME)
        cutoff = time.time() - 30 * 86400
        params = (user_id, layer, min_weight, cutoff)
        doomed = await (
            await conn.execute(
                "SELECT episode_id, summary, emotional_weight FROM episodes WHERE user_id=? AND layer=? AND emotional_weight < ? AND created_at < ?",
                params,
            )
        ).fetchall()
        if not doomed:
            return 0

        # Archive before delete — the tool promises "archive", not erasure.
        from shared.archived_memories import ArchivedMemories

        am = ArchivedMemories(cm=self._cm)
        await am._init_db()
        for r in doomed:
            await am.archive(
                user_id=user_id,
                content=r["summary"],
                memory_type="episode",
                importance=float(r["emotional_weight"]),
                original_id=int(r["episode_id"]),
                reason="compression_low_weight",
            )

        cursor = await conn.execute(
            "DELETE FROM episodes WHERE user_id=? AND layer=? AND emotional_weight < ? AND created_at < ?",
            params,
        )
        await conn.commit()
        return cast("int", cursor.rowcount)

    async def get_stats(self, user_id: str | None = None) -> dict[str, int]:
        stats: dict[str, int] = {}
        # Explicit static queries for security audit (SKY-D211 bypass);
        # second tuple element = query accepts a user_id parameter.
        queries: dict[str, tuple[str, bool]] = {
            "core_memory": ("SELECT COUNT(*) FROM core_memory WHERE user_id=?", True),
            "episodes": ("SELECT COUNT(*) FROM episodes WHERE user_id=?", True),
            "agent_wiki": ("SELECT COUNT(*) FROM agent_wiki", False),
            "user_wiki": ("SELECT COUNT(*) FROM user_wiki", False),
            "file_wiki": ("SELECT COUNT(*) FROM file_wiki", False),
        }

        conn = await self._cm.get(DB_NAME)
        for name, (sql, filtered) in queries.items():
            try:
                row = await (await conn.execute(sql, (user_id,) if filtered and user_id else ())).fetchone()
                stats[name] = row[0] if row else 0
            except Exception as e:
                # Absent tables (e.g. file_wiki) are normal; anything else
                # deserves visibility instead of a healthy-looking zero.
                logger.warning("stats[%s] failed: %s", name, e)
                stats[name] = 0
        return stats
