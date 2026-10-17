from __future__ import annotations

from typing import TYPE_CHECKING

from shared.constants import DB_NAME

from repositories.base import BaseRepo

if TYPE_CHECKING:
    from collections.abc import Sequence


class MemoryRepo(BaseRepo):
    """Owns core_memory, core_memory_history, core_memory_temporal, archived_memories."""

    async def decay_batch(self, updates: Sequence[tuple[float, int]]) -> None:
        """Apply importance updates; the dispatch commits the batch atomically."""
        ops = [("UPDATE core_memory SET importance = ? WHERE entry_id = ?", (imp, eid)) for imp, eid in updates]
        if ops:
            await self._dispatch.write_many(DB_NAME, ops)
