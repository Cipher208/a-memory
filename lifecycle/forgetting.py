from __future__ import annotations

"""
Forgetting System — type-aware decay, archiving, compression
"""

import contextlib
import logging
import time
import os
from dataclasses import dataclass, field
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

ARCHIVE_DIR = Path(os.environ.get("MCP_MEMORY_DATA_DIR") or str(Path.home() / ".mcp-ariel-memory")) / "archives"

#: Archived `memory_type` values a restore is allowed to route. `episode` is the
#: episodic marker rather than a `MemoryKind` — episodes live in their own table —
#: so it is named here explicitly. A kind outside this set is refused with a
#: reason, never written into `core_memory` as an invalid `memory_kind`.
#:
#: Measured on the base on 2026-10-11: every archived `memory_type` is either
#: `episode` or a real `MemoryKind`, and the one value that is neither —
#: `core_memory`, a table name leaked into a kind field — appears only on purge
#: rows, which the refusal below catches first. This set is therefore a guard
#: against the future rather than a live workaround.
ROUTABLE_ARCHIVE_KINDS = frozenset({k.value for k in MemoryKind}) | {"episode"}


@dataclass
class RestoreOutcome:
    """What a restore did, and what it refused to do.

    A bare count cannot carry a refusal, and the refusals are the point of this
    method: a row removed deliberately, an id that names two rows, a kind that
    cannot be routed. Returning `0` for all three is the silence this repair
    exists to end, so each gets its own list and its own reason.
    """

    restored: int = 0
    #: Refused ids, each `{"original_id": int, "reason": "purged" | "ambiguous" | "unsupported_kind", ...}`.
    refused: list[dict[str, Any]] = field(default_factory=list)
    #: Ids that name no archived row for this owner and kind.
    not_found: list[int] = field(default_factory=list)


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

    async def archive_entries(self, ids: list[int]) -> int:
        """Archive + delete exactly these core entry ids (C1.11 staged apply).

        Best-effort: ids that no longer exist are skipped. Pins the proposal's
        id list — no re-scan, no drift between propose-time and apply-time.
        """
        if not ids:
            return 0
        try:
            conn = await self._cm.get(DB_NAME)
            now = time.time()
            placeholders = ",".join(["?"] * len(ids))
            cur = await conn.execute(f"SELECT * FROM core_memory WHERE entry_id IN ({placeholders})", tuple(ids))
            rows = await cur.fetchall()
            if not rows:
                return 0
            archived_count = await self._perform_archiving(rows, now)
            await self._delete_archived(conn, rows)
            await conn.commit()
            return archived_count
        except Exception:
            logger.exception("archive_entries failed")
            return 0

    async def restore_entries(self, ids: list[int], user_id: str, memory_type: str | None = None) -> RestoreOutcome:
        """Bring archived rows back, matched on owner and kind (C1.13 revert).

        WHY THE MATCH IS NO LONGER ONE FIELD. This used to be
        `WHERE original_id = ?`. `original_id` is an integer shared by two tables
        that number independently — `episodes.episode_id` and
        `core_memory.entry_id` — and the archive holds the kind in `memory_type`,
        which the query never looked at. On the base measured 2026-10-11, 870 id
        values existed in both tables and 10 `original_id` values already carried
        two archived rows of different kinds under one number. `fetchone()` took
        whichever it met first, and the row was written into `core_memory`
        whatever it had been.

        **The owner is required, not defaulted.** The old signature had no owner
        at all, which is what kept a cross-user restore latent rather than
        impossible. A default would put the same hole back one argument to the
        left, so there is none.

        **Routing happens before the content is parsed.** An archived episode
        holds a bare summary; an L4 row holds `key=value`. Parsing first would
        cut an episode whose text contains `=` at that character and file the
        halves as a key and a value.

        WHAT CANNOT BE RESTORED, returned as a reason rather than a silent zero:

        - a row whose `archive_reason` is a purge reason — a deliberate removal is
          not a forgetting, and this has no off switch (see `PURGE_REASONS`);
        - an id matching **more than one** row for this owner when no kind was
          named — refused instead of guessed, which is the behaviour the old
          `fetchone()` had;
        - a kind that is not routable, so an invalid `memory_kind` never reaches
          `core_memory`.

        WHAT IS LOST. `layer` is not in the archive and neither are an episode's
        `tags`, `created_at` or its layer, so a restored episode lands in this
        system's own layer and carries the moment of its return. Data the archive
        never kept cannot be restored, and this method does not pretend it can.
        The rule is settled rather than pending: separating that metadata is
        separate work, deliberately not done here.
        """
        from shared.archived_memories import is_restorable_reason

        outcome = RestoreOutcome()
        if not ids:
            return outcome
        try:
            conn = await self._cm.get(DB_NAME)
            now = time.time()
            for original_id in ids:
                sql = "SELECT * FROM archived_memories WHERE original_id = ? AND user_id = ?"
                params: list[Any] = [original_id, user_id]
                if memory_type is not None:
                    sql += " AND memory_type = ?"
                    params.append(memory_type)
                rows = await (await conn.execute(sql, tuple(params))).fetchall()
                if not rows:
                    outcome.not_found.append(original_id)
                    continue
                if len(rows) > 1:
                    outcome.refused.append(
                        {
                            "original_id": original_id,
                            "reason": "ambiguous",
                            "kinds": sorted({str(r["memory_type"]) for r in rows}),
                        }
                    )
                    continue
                row = rows[0]
                # Checked before the kind is trusted: a purge row is refused
                # whatever it claims to be, which is what keeps the 41% out of
                # reach even when a caller names the id and the kind.
                if not is_restorable_reason(row["archive_reason"]):
                    outcome.refused.append({"original_id": original_id, "reason": "purged"})
                    continue
                kind = str(row["memory_type"] or "fact")
                if kind not in ROUTABLE_ARCHIVE_KINDS:
                    outcome.refused.append({"original_id": original_id, "reason": "unsupported_kind", "memory_type": kind})
                    continue
                if kind == "episode":
                    await self._restore_episode(row, user_id)
                else:
                    await self._restore_core(conn, row, original_id, now)
                await conn.execute("DELETE FROM archived_memories WHERE id = ?", (row["id"],))
                outcome.restored += 1
            await conn.commit()
            return outcome
        except Exception:
            logger.exception("restore_entries failed")
            # The partial outcome, not a zero: whatever landed before the failure
            # did land, and saying otherwise would be the same silence in reverse.
            return outcome

    async def _restore_episode(self, row: Any, user_id: str) -> int:
        """Put an archived episode back into `episodes`, where it came from.

        `EpisodicMemory.save()` dedups on `(layer, user_id, summary)` and returns
        the existing id when the text is already present, so restoring an episode
        whose summary was re-added is a no-op on the row count. That is counted as
        restored — the text is in the table, which is what the caller asked for —
        and it is a deliberate reading rather than an accident of the dedup.
        """
        from core.episodic import EpisodicMemory

        mem = EpisodicMemory(cm=self._cm, layer=self.layer)
        return await mem.save(user_id, str(row["content"]), emotional_weight=float(row["importance"] or 0.5))

    async def _restore_core(self, conn: Any, row: Any, original_id: int, now: float) -> None:
        """Put an archived L4 row back into `core_memory`.

        `partition` splits on the first `=` only, so a value that contains one
        survives whole: `k=v=w` returns key `k`, value `v=w`.
        """
        key, _, value = str(row["content"]).partition("=")
        await conn.execute(
            "INSERT INTO core_memory (user_id, key, value, importance, created_at, updated_at, memory_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["user_id"],
                key or f"restored_{original_id}",
                value or str(row["content"]),
                row["importance"],
                now,
                now,
                row["memory_type"] or "fact",
            ),
        )

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
