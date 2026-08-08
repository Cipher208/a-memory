from __future__ import annotations
"""
Consolidation Engine — L1→L2→L3→L4 memory promotion (async)
Type-aware promotion with memory_kind support.
"""

from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.memory_types import MemoryKind, get_policy, validate_kind


class ConsolidationEngine:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def consolidate_staging(
        self,
        user_id: str,
        staging_items: list[dict[str, Any]],
        min_importance: float = 0.7,
    ) -> dict[str, int]:
        """Type-aware promotion: instruction/rule/commitment pass even with low importance."""
        from core.memory import CoreMemory

        cm = CoreMemory(cm=self._cm)
        promoted = 0
        skipped = 0

        for item in staging_items:
            content = item.get("content", "")
            importance = float(item.get("importance", 0.7))
            kind_str = item.get("memory_kind", "fact")

            kind = MemoryKind(kind_str) if validate_kind(kind_str) else MemoryKind.FACT
            pol = get_policy(kind)

            # Type-aware threshold: instruction/rule/commitment pass at 0.3+
            effective_threshold = (
                min_importance
                if not (pol.never_archive or kind in (MemoryKind.INSTRUCTION, MemoryKind.RULE, MemoryKind.COMMITMENT))
                else min(min_importance, 0.3)
            )
            if importance < effective_threshold:
                skipped += 1
                continue

            key = "staging_{}".format(content[:30].replace(" ", "_").lower())
            await cm.save(
                user_id,
                key,
                content,
                importance=importance,
                memory_kind=kind_str,
                source="staging_promotion",
            )
            promoted += 1

        return {"promoted": promoted, "skipped": skipped}

    async def consolidate_episodes(
        self,
        user_id: str,
        episodic_db: str | None = None,
        min_weight: float = 0.7,
    ) -> int:
        from core.memory import CoreMemory

        cm = CoreMemory(cm=self._cm)
        epi_db = episodic_db or "memory.db"
        epi_conn = await self._cm.get(epi_db)
        cursor = await epi_conn.execute(
            "SELECT summary, emotional_weight, memory_kind FROM episodes WHERE user_id=? AND emotional_weight > ? ORDER BY created_at DESC LIMIT 10",
            (user_id, min_weight),
        )
        rows = await cursor.fetchall()

        if not rows:
            return 0

        consolidated = 0
        for row in rows:
            summary = row["summary"]
            weight = row["emotional_weight"]
            kind = row["memory_kind"] or "fact"
            key = "ep_{}".format(summary[:30].replace(" ", "_").lower())
            await cm.save(user_id, key, summary[:200], importance=weight, memory_kind=kind, source="episode_promotion")
            consolidated += 1
        return consolidated

    async def get_stats(self, user_id: str) -> dict[str, int]:
        conn = await self._cm.get(DB_NAME)
        total_cursor = await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id=?", (user_id,))
        total = (await total_cursor.fetchone())[0]
        high_cursor = await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id=? AND importance > 0.7", (user_id,))
        high = (await high_cursor.fetchone())[0]
        low_cursor = await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id=? AND importance < 0.3", (user_id,))
        low = (await low_cursor.fetchone())[0]
        return {"total": total, "high_importance": high, "low_importance": low}
