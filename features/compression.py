from __future__ import annotations

"""
MemoryCompressor — async dedup and compression
"""

import time

from typing import cast
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


class MemoryCompressor:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def deduplicate_core(self, user_id: str) -> int:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT user_id, key, COUNT(*) as cnt FROM core_memory WHERE user_id=? GROUP BY user_id, key HAVING cnt > 1",
            (user_id,),
        )
        duplicates = await cursor.fetchall()
        removed = 0
        for dup in duplicates:
            cursor = await conn.execute(
                """DELETE FROM core_memory WHERE user_id=? AND key=? AND entry_id NOT IN
                   (SELECT entry_id FROM core_memory WHERE user_id=? AND key=? ORDER BY updated_at DESC LIMIT 1)""",
                (dup["user_id"], dup["key"], dup["user_id"], dup["key"]),
            )
            removed += cursor.rowcount
        await conn.commit()
        return removed

    async def compress_episodes(self, user_id: str, min_weight: float = 0.3) -> int:
        conn = await self._cm.get(DB_NAME)
        cutoff = time.time() - 30 * 86400
        cursor = await conn.execute(
            "DELETE FROM episodes WHERE user_id=? AND emotional_weight < ? AND created_at < ?",
            (user_id, min_weight, cutoff),
        )
        await conn.commit()
        return cast("int", cursor.rowcount)

    async def get_stats(self, user_id: str | None = None) -> dict[str, int]:
        stats = {}
        # Explicit static queries for security audit (SKY-D211 bypass)
        queries = {
            "core_memory": "SELECT COUNT(*) FROM core_memory",
            "episodes": "SELECT COUNT(*) FROM episodes",
            "agent_wiki": "SELECT COUNT(*) FROM agent_wiki",
            "user_wiki": "SELECT COUNT(*) FROM user_wiki",
            "file_wiki": "SELECT COUNT(*) FROM file_wiki",
        }

        conn = await self._cm.get(DB_NAME)
        for name, sql in queries.items():
            try:
                # Static SQL strings only, parameters handled via standard DB-API if needed
                row = await (await conn.execute(sql)).fetchone()
                stats[name] = row[0] if row else 0
            except Exception:
                stats[name] = 0
        return stats
