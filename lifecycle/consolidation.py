from __future__ import annotations

"""
Consolidation Engine — L1→L2→L3→L4 memory promotion (async)
Type-aware promotion with memory_kind support.
"""

import json
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.memory_types import MemoryKind, get_policy, validate_kind

# B1.4: provenance of derived summaries. A promoted fact stores its source
# references in core_memory.metadata["parents"] as "<kind>:<id>" strings —
# a DAG (a fact may merge several parents), no schema change required.


def _parent_refs(*refs: str | None) -> dict[str, Any] | None:
    parents = [r for r in refs if r]
    return {"parents": parents} if parents else None


class ConsolidationEngine:
    def __init__(self, cm: AsyncConnectionManager | None = None, layer: str = "user"):
        self._cm = cm or connection_manager
        self.layer = layer

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
                metadata=_parent_refs(f"event:{item['event_id']}" if item.get("event_id") else None),
            )
            promoted += 1

        return {"promoted": promoted, "skipped": skipped}

    async def consolidate_episodes(
        self,
        user_id: str,
        episodic_db: str | None = None,
        min_weight: float = 0.7,
    ) -> int:
        """Promote high-weight episodes of THIS engine's layer into L4 facts.

        Idempotent: the L4 key is derived from the summary, re-runs update
        in place instead of duplicating.
        """
        from core.memory import CoreMemory

        cm = CoreMemory(cm=self._cm, layer=self.layer)
        epi_db = episodic_db or "memory.db"
        epi_conn = await self._cm.get(epi_db)
        cursor = await epi_conn.execute(
            "SELECT episode_id, summary, emotional_weight, tags FROM episodes WHERE layer=? AND user_id=? AND emotional_weight > ? ORDER BY created_at DESC LIMIT 10",
            (self.layer, user_id, min_weight),
        )
        rows = await cursor.fetchall()

        if not rows:
            return 0

        consolidated = 0
        for row in rows:
            summary = row["summary"]
            weight = row["emotional_weight"]
            key = "ep_{}".format(summary[:30].replace(" ", "_").lower())
            await cm.save(
                user_id,
                key,
                summary[:200],
                importance=weight,
                memory_kind="fact",
                source="episode_promotion",
                metadata=_parent_refs(f"episode:{row['episode_id']}"),
            )
            consolidated += 1
        return consolidated

    async def get_lineage(self, entry_id: int) -> list[str]:
        """Return the parent references recorded for a promoted fact (B1.4)."""
        conn = await self._cm.get(DB_NAME)
        row = await (
            await conn.execute("SELECT metadata FROM core_memory WHERE entry_id=?", (entry_id,))
        ).fetchone()
        if not row or not row["metadata"]:
            return []
        try:
            meta = json.loads(row["metadata"])
        except (TypeError, ValueError):
            return []
        parents = meta.get("parents", []) if isinstance(meta, dict) else []
        return [str(p) for p in parents if p]

    async def get_stats(self, user_id: str) -> dict[str, int]:
        conn = await self._cm.get(DB_NAME)
        total_cursor = await conn.execute("SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=?", (self.layer, user_id))
        total_row = await total_cursor.fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0

        high_cursor = await conn.execute(
            "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=? AND importance > 0.7",
            (self.layer, user_id),
        )
        high_row = await high_cursor.fetchone()
        high = int(high_row[0]) if high_row and high_row[0] is not None else 0

        low_cursor = await conn.execute(
            "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=? AND importance < 0.3",
            (self.layer, user_id),
        )
        low_row = await low_cursor.fetchone()
        low = int(low_row[0]) if low_row and low_row[0] is not None else 0
        return {"total": total, "high_importance": high, "low_importance": low}
