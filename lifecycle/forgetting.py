from __future__ import annotations

"""
Forgetting System — type-aware decay, archiving, compression
"""

import contextlib
import logging
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from config import config
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.memory_types import (
    MemoryKind,
    apply_decay,
    validate_kind,
)

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)

ARCHIVE_DIR = Path.home() / ".mcp-ariel-memory" / "archives"


class ForgettingSystem:
    def __init__(self, cm: AsyncConnectionManager | None = None, layer: str = "user"):
        self._cm = cm or connection_manager
        self.layer = layer
        self.decay_rate = config.get_forgetting("decay_rate") or 0.01
        self.archive_days = config.get_forgetting("archive_threshold_days") or 90
        self.archive_min_importance = config.get_forgetting("archive_min_importance") or 0.3
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    async def decay_importance(self) -> int:
        """Type-aware decay: instruction/rule/commitment never decay (decay_rate=0)."""
        try:
            now = time.time()
            conn = await self._cm.get(DB_NAME)
            cursor = await conn.execute("SELECT entry_id, memory_kind, importance, updated_at FROM core_memory")

            updates: list[tuple[float, int]] = []
            while True:
                rows = await cursor.fetchmany(1000)
                if not rows:
                    break
                for r in rows:
                    kind_str = r["memory_kind"] or "fact"
                    if not validate_kind(kind_str):
                        continue
                    kind = MemoryKind(kind_str)
                    days = (now - float(r["updated_at"])) / 86400.0
                    new_imp = apply_decay(float(r["importance"]), kind, days)
                    if abs(new_imp - float(r["importance"])) > 1e-3:
                        updates.append((new_imp, int(r["entry_id"])))

            if not updates:
                return 0

            await conn.executemany(
                "UPDATE core_memory SET importance = ? WHERE entry_id = ?",
                updates,
            )
            await conn.commit()
            logger.info("Decayed %d entries", len(updates))
            return len(updates)
        except Exception:
            logger.exception("Decay failed")
            return 0

    async def archive_old_entries(self) -> int:
        """Type-aware archive: instruction/rule/commitment never archived.

        Goal/todo/commitment archived by expires_at. Others by age + importance.
        """
        try:
            conn = await self._cm.get(DB_NAME)
            now = time.time()

            # 1. Find archivable items
            all_rows = await self._find_archivable_entries(conn, now)
            if not all_rows:
                return 0

            # 2. Perform archiving
            archived_count = await self._perform_archiving(all_rows, now)

            # 3. Safe deletion
            await self._delete_archived(conn, all_rows)

            await conn.commit()
            logger.info("Archived %d entries", archived_count)
            return archived_count
        except Exception:
            logger.exception("Archive failed")
            return 0

    async def _find_archivable_entries(self, conn: Any, now: float) -> list[sqlite3.Row]:
        # Expired goals/todos/commitments
        expired = await (
            await conn.execute(
                """SELECT entry_id, user_id, key, value, memory_kind, importance, expires_at
               FROM core_memory
               WHERE memory_kind IN ('goal', 'todo', 'commitment')
                 AND expires_at IS NOT NULL AND expires_at < ?""",
                (now,),
            )
        ).fetchall()

        # Old low-importance entries (excluding never-archive types)
        old = await (
            await conn.execute(
                """SELECT entry_id, user_id, key, value, memory_kind, importance, expires_at
               FROM core_memory
               WHERE memory_kind NOT IN ('instruction', 'rule', 'commitment')
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND updated_at < ?
                 AND importance < ?""",
                (now, now - self.archive_days * 86400, self.archive_min_importance),
            )
        ).fetchall()

        return list(expired) + list(old)

    async def _perform_archiving(self, rows: list[sqlite3.Row], now: float) -> int:
        from shared.archived_memories import ArchivedMemories

        am = ArchivedMemories(cm=self._cm)
        count = 0
        for r in rows:
            archived_id = await am.archive(
                user_id=r["user_id"],
                content="{}={}".format(r["key"], r["value"]),
                memory_type=r["memory_kind"] or "fact",
                importance=r["importance"],
                original_id=r["entry_id"],
                reason="expired" if (r["expires_at"] and r["expires_at"] < now) else f"inactive_{self.archive_days}d",
            )
            with contextlib.suppress(Exception):
                from lifecycle.transitions import record_transition

                await record_transition(
                    self._cm,
                    r["user_id"],
                    "l4",
                    f"core:{r['entry_id']}",
                    "archived",
                    f"archived:{archived_id}",
                    "expired" if (r["expires_at"] and r["expires_at"] < now) else f"inactive_{self.archive_days}d",
                )
            count += 1
        return count

    async def _delete_archived(self, conn: Any, rows: list[sqlite3.Row]) -> None:
        ids = [r["entry_id"] for r in rows]
        placeholders = ",".join(["?"] * len(ids))
        await conn.execute(f"DELETE FROM core_memory WHERE entry_id IN ({placeholders})", tuple(ids))

    async def compress_duplicates(self) -> int:
        try:
            conn = await self._cm.get(DB_NAME)
            # Same key in DIFFERENT layers is not a duplicate — dedupe per layer.
            # Single set-based DELETE: keep the newest row per (layer, user_id, key),
            # remove the rest in one statement instead of per-key round trips.
            cursor = await conn.execute(
                """DELETE FROM core_memory WHERE entry_id IN (
                       SELECT entry_id FROM (
                           SELECT entry_id,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY layer, user_id, key
                                      ORDER BY updated_at DESC, entry_id DESC
                                  ) AS rn
                           FROM core_memory
                       )
                       WHERE rn > 1
                   )""",
            )
            removed = int(cursor.rowcount or 0)
            await conn.commit()
            return removed
        except Exception:
            logger.exception("Compression failed")
            return 0

    async def cleanup(self) -> dict[str, int]:
        return {
            "decayed": await self.decay_importance(),
            "archived": await self.archive_old_entries(),
            "compressed": await self.compress_duplicates(),
        }

    async def run_cleanup(self, user_id: str = "default") -> dict[str, int]:
        """Alias for archive_old_entries to satisfy legacy compactor interface."""
        # ForgettingSystem defaults are used, user_id can override if needed
        # (though ForgettingSystem is currently layer-scoped)
        count = await self.archive_old_entries()
        return {"archived": count}


# Global instance
forgetting_system = ForgettingSystem()
