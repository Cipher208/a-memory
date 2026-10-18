from __future__ import annotations

"""
MemoryCompressor — async dedup and compression
"""

import contextlib
import logging
import time

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch, get_write_dispatch

logger = logging.getLogger(__name__)


class MemoryCompressor:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    @property
    def _dispatch(self) -> WriteDispatch:
        """Resolved on every access so a repointed ``_cm`` is honoured."""
        return get_write_dispatch(self._cm)

    async def deduplicate_core(self, user_id: str) -> int:
        """Legacy-safe same-key dedup per (layer, user_id).

        Current schemas enforce UNIQUE(layer, user_id, key), so on live DBs
        this removes 0 rows; it only cleans databases created before that
        index existed. Partitioning BY layer is essential: a user fact and
        an agent fact sharing a key are legitimate distinct memories.
        """
        return await self._dispatch.write(
            DB_NAME,
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

    async def compress_episodes(self, user_id: str, min_weight: float = 0.3, layer: str = "user") -> int:
        """Drop old low-weight episodes of ONE layer, archiving them first.

        Layer-scoped deliberately: agent-layer episodes are identity data
        and must never be swept by user-memory maintenance.
        """
        cutoff = time.time() - 30 * 86400
        params = (user_id, layer, min_weight, cutoff)
        doomed = await self._dispatch.query(
            DB_NAME,
            "SELECT episode_id, summary, emotional_weight FROM episodes WHERE user_id=? AND layer=? AND emotional_weight < ? AND created_at < ?",
            params,
        )
        if not doomed:
            return 0

        # Archive before delete — the tool promises "archive", not erasure.
        from shared.archived_memories import ArchivedMemories

        am = ArchivedMemories(cm=self._cm)
        await am._init_db()
        for r in doomed:
            archived_id = await am.archive(
                user_id=user_id,
                content=r["summary"],
                memory_type="episode",
                importance=float(r["emotional_weight"]),
                original_id=int(r["episode_id"]),
                reason="compression_low_weight",
            )
            with contextlib.suppress(Exception):
                from lifecycle.transitions import record_transition

                await record_transition(
                    self._cm, user_id, "episode", f"episode:{r['episode_id']}", "archived", f"archived:{archived_id}", "compression_low_weight"
                )

        return int(
            await self._dispatch.write(
                DB_NAME,
                "DELETE FROM episodes WHERE user_id=? AND layer=? AND emotional_weight < ? AND created_at < ?",
                params,
            )
        )

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

        for name, (sql, filtered) in queries.items():
            try:
                value = await self._dispatch.scalar(DB_NAME, sql, (user_id,) if filtered and user_id else ())
                stats[name] = int(value) if value is not None else 0
            except Exception as e:
                # Absent tables (e.g. file_wiki) are normal; anything else
                # deserves visibility instead of a healthy-looking zero.
                logger.warning("stats[%s] failed: %s", name, e)
                stats[name] = 0
        return stats
