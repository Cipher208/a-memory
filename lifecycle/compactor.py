"""
Memory Compactor — handles long-term memory maintenance.
Finds old, low-importance memories and moves them to archive or summarizes them.
"""

import logging
import time

from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


class MemoryCompactor:
    """Maintenance engine for memory cleanup and summarization."""

    def __init__(self, age_days: int = 7, min_importance: float = 0.4):
        self.age_days = age_days
        self.min_importance = min_importance

    async def run_cleanup(self, user_id: str = "default") -> dict[str, int]:
        """Move old, unimportant memories to archive."""
        cutoff_time = time.time() - (self.age_days * 86400)

        conn = await connection_manager.get(DB_NAME)

        # 1. Find candidates in core_memory
        cur = await conn.execute(
            """SELECT entry_id as id, value as content, memory_kind as memory_type, importance FROM core_memory
               WHERE user_id=? AND importance < ? AND created_at < ?""",
            (user_id, self.min_importance, cutoff_time),
        )
        candidates = await cur.fetchall()

        if not candidates:
            return {"archived": 0}

        archived_count = 0
        for row in candidates:
            try:
                # 2. Insert into archive
                await conn.execute(
                    """INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, row["id"], row["content"], row["memory_type"], row["importance"], "low_importance_old"),
                )

                # 3. Delete from active memory
                await conn.execute("DELETE FROM core_memory WHERE entry_id=?", (row["id"],))
                archived_count += 1
            except (KeyError, RuntimeError):  # noqa: PERF203
                logger.exception("Failed to archive memory %s", row.get("id", "unknown"))

        await conn.commit()
        logger.info("Memory compaction: archived %d memories for user %s", archived_count, user_id)

        # Update metrics
        from shared.metrics import metrics

        metrics.memory_ops_total.labels(action="compaction_archive", layer=user_id).inc(archived_count)

        return {"archived": archived_count}

    async def run_summarization(self, user_id: str = "default") -> None:
        """TODO: Implement LLM-based summarization for groups of related old memories."""


# Global instance
memory_compactor = MemoryCompactor()
