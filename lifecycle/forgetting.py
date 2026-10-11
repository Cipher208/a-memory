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

#: Which archived kinds a restore may route used to be decided **here**, by an
#: allowlist built from `MemoryKind`.
#:
#: This used to be `frozenset(MemoryKind) | {"episode"}`, on the assumption that the
#: kind column is constrained by the enum. **It is not** — `core_memory.memory_kind`
#: is a bare `TEXT` with no CHECK, and measuring one base hid the others: `log` (1
#: row) and `bug` (3 rows) exist in `core_memory` and the enum has never heard of
#: either, so the allowlist refused rows that legitimately came from `core_memory`.
#: The rule now lives in `shared/archived_memories.py` as `route_of`, as a denylist
#: of the sources that are *not* `core_memory`, and the restore and the listing ask
#: that one function so they cannot drift apart.


@dataclass
class RestoreOutcome:
    """What a restore did, and what it refused to do.

    A bare count cannot carry a refusal, and the refusals are the point of this
    method: a row removed deliberately, an id that names two rows, a kind that
    cannot be routed. Returning `0` for all three is the silence this repair
    exists to end, so each gets its own list and its own reason.
    """

    restored: int = 0
    #: Refused ids, each `{"original_id": int, "reason": "purged" | "unroutable" | "ambiguous", ...}`
    #: with `memory_type` attached, and `kinds` on an ambiguous one. The reasons are
    #: the same names the listing counts in `omitted_by_reason`.
    refused: list[dict[str, Any]] = field(default_factory=list)
    #: Ids that name no archived row for this owner and kind.
    not_found: list[int] = field(default_factory=list)
    #: One entry per row this call **resolved** — restored, or that would be restored
    #: under `dry_run`. Carries the destination and, per row, the fields of the target
    #: table the archive never kept. A caller cannot see the destination from an id,
    #: and `not_recorded` is the limit of `undo` stated where the answer is read.
    restored_items: list[dict[str, Any]] = field(default_factory=list)
    #: True when nothing was written. `restored` is then 0 while `restored_items`
    #: still describes the plan — the same contract `forget`'s own preview keeps.
    dry_run: bool = False


#: Which columns of the destination table an archived row **can** fill. The archive keeps
#: eight columns (`user_id`, `original_id`, `content`, `memory_type`, `importance`,
#: `archive_reason`, `archived_at`, `id`); `episodes` and `core_memory` have more, and the
#: difference is what a restore cannot bring back.
_CARRIED_INTO_EPISODES = frozenset({"user_id", "summary", "emotional_weight"})
_CARRIED_INTO_CORE = frozenset({"user_id", "key", "value", "importance", "memory_kind"})


async def _not_carried(conn: Any, table: str, carried: frozenset[str]) -> set[str]:
    """Return the columns of `table` an archive row cannot fill — **read from the schema**.

    Not a list written by hand, and that is the point of asking `PRAGMA` instead: a
    hand-written account of "what is lost" goes stale the first time somebody adds a
    column, and it is exactly the kind of list a caller believes without checking.
    Measured 2026-10-11 on a migrated database, this answers `episode_id, tags,
    created_at, memory_kind, layer` for an episode and nine names for a core row,
    including `expires_at`, `source`, `metadata` and `visibility`.

    `table` is never caller input: it is one of two literals this module chooses from
    the row's route, which is why an interpolated `PRAGMA` is safe here.
    """
    rows = await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
    return {r["name"] for r in rows} - carried


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

    async def restore_entries(self, ids: list[int], user_id: str, memory_type: str | None = None, dry_run: bool = False) -> RestoreOutcome:
        """Bring archived rows back, matched on owner and kind (C1.13 revert).

        `dry_run=True` runs the same matching loop and writes nothing: no `save`, no
        `INSERT`, no `DELETE`. The preview is the pass, so it cannot drift from it —
        the same contract `forget`'s own dry run keeps on the other end. `restored`
        is then 0, because nothing was restored, and `restored_items` still names
        every row the real call would return.

        WHY THE MATCH IS NO LONGER ONE FIELD. This used to be
        `WHERE original_id = ?`. `original_id` is an integer shared by two tables
        that number independently — `episodes.episode_id` and `core_memory.entry_id`
        — and the archive holds the kind in `memory_type`, which the query never
        looked at. `fetchone()` took whichever it met first, and the row was written
        into `core_memory` whatever it had been.

        **How often they collide is a property of the base, so the number must not be
        memorised.** Measured 2026-10-11 from copies: the `-cowagent` base held 904 id
        values in both tables and **108** `original_id` values carrying two archived
        rows of different kinds for one owner; the `-hermes` base held 17 and **738**.
        An earlier version of this docstring quoted one pair as though it were the
        truth about "the" base; it was not reproducible on either, because there is no
        single base — each instance has its own archive.

        **The owner is required, not defaulted.** The old signature had no owner
        at all, which is what kept a cross-user restore latent rather than
        impossible. A default would put the same hole back one argument to the
        left, so there is none.

        **Routing happens before the content is parsed.** An archived episode
        holds a bare summary; an L4 row holds `key=value`. Parsing first would
        cut an episode whose text contains `=` at that character and file the
        halves as a key and a value.

        WHAT CANNOT BE RESTORED, returned as a reason rather than a silent zero. A
        row's *reason and kind* are judged by `refusal_reason` in
        `shared.archived_memories.py` — **the same function the listing asks** — so
        the bin cannot be filtered on a different rule from the one this applies:

        - a row whose `archive_reason` is a purge reason — a deliberate removal is
          not a forgetting, and this has no off switch (see `PURGE_REASONS`);
        - a row whose recorded kind names no table this can write to: a `NULL` kind,
          which means the writer did not say, and the labels with no restore path in
          this repository (`graph:<node_type>` from `forget`'s shadow bin, and
          `project_archive`, a **wiki page** body from `project.py`). Nor is a default
          substituted for a missing kind: a guessed `fact` is the same guess
          `fetchone()` used to make about which row it had found;
        - an id matching **more than one** row for this owner, refused instead of
          picked. Naming the kind narrows the match, so in practice this means an
          unnamed kind over an id that two tables happen to share — the old
          `fetchone()` behaviour, returned as a refusal.

        The first two are decided from the row alone, which is why the listing can
        pre-filter them. The third is not — it depends on the call — so a row the
        listing offers can still be refused, and the `memory_type` the listing returns
        is the argument that resolves it rather than a contradiction of it.

        WHAT IS LOST. `layer` is not in the archive and neither are an episode's
        `tags`, `created_at` or its layer, so a restored episode lands in this
        system's own layer and carries the moment of its return. Data the archive
        never kept cannot be restored, and this method does not pretend it can.
        The rule is settled rather than pending: separating that metadata is
        separate work, deliberately not done here.
        """
        from shared.archived_memories import ROUTE_EPISODES, refusal_reason, route_of

        outcome = RestoreOutcome()
        if not ids:
            return outcome
        try:
            conn = await self._cm.get(DB_NAME)
            now = time.time()
            outcome.dry_run = dry_run
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
                    # The kinds are reported as they are recorded, `None` included:
                    # `str(None)` here would print the string `"None"` and invent a
                    # kind that is not in the table — a small lie in the one payload
                    # whose whole job is to say precisely which rows collided.
                    outcome.refused.append(
                        {
                            "original_id": original_id,
                            "reason": "ambiguous",
                            "kinds": sorted({r["memory_type"] for r in rows}, key=lambda k: (k is None, str(k))),
                        }
                    )
                    continue
                row = rows[0]
                # The refusal is decided by the same function the listing asks, so
                # the listing and the restore cannot disagree about a reason or a kind.
                # It is asked before the kind is acted on: a purge row is refused
                # whatever it claims to be, which is what keeps the 41% out of reach
                # even when a caller names the id and the kind.
                why = refusal_reason(row["archive_reason"], row["memory_type"])
                if why is not None:
                    outcome.refused.append(
                        {
                            "original_id": original_id,
                            "reason": why,
                            "memory_type": row["memory_type"],
                        }
                    )
                    continue
                if route_of(row["memory_type"]) == ROUTE_EPISODES:
                    destination, carried = "episodes", _CARRIED_INTO_EPISODES
                else:
                    destination, carried = "core_memory", _CARRIED_INTO_CORE
                if not dry_run:
                    if destination == "episodes":
                        await self._restore_episode(row, user_id)
                    else:
                        await self._restore_core(conn, row, original_id, now)
                    await conn.execute("DELETE FROM archived_memories WHERE id = ?", (row["id"],))
                    outcome.restored += 1
                outcome.restored_items.append(
                    {
                        "original_id": original_id,
                        "memory_type": row["memory_type"],
                        "destination": destination,
                        # The limit, per row, where the answer is read. A caller who
                        # gets a summary back and no tags should learn why from this
                        # answer rather than from a missing tag weeks later.
                        "not_recorded": sorted(await _not_carried(conn, destination, carried)),
                    }
                )
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
        # `None` is checked, not truthiness. `row["importance"] or 0.5` — the earlier
        # form — treats a recorded `0.0` as absent and returns the episode with a
        # different weight than it had. **This was live, not latent.** An earlier
        # version of this comment said the base held no such row; that was measured on
        # one instance and is false of the set of them. Measured 2026-10-11 from copies:
        # the `-hermes` base holds one archived episode with `importance=0.0` and reason
        # `forget_primitive_fuzzy_l3`, so the old form would have restored that episode
        # at 0.5. There is no single base to measure — the archive is per instance.
        weight = 0.5 if row["importance"] is None else float(row["importance"])
        return await mem.save(user_id, str(row["content"]), emotional_weight=weight)

    async def _restore_core(self, conn: Any, row: Any, original_id: int, now: float) -> None:
        """Put an archived L4 row back into `core_memory`.

        `partition` splits on the first `=` only, so a value that contains one
        survives whole: `k=v=w` returns key `k`, value `v=w`.

        The kind is written back **exactly as recorded**. An earlier version
        substituted `"fact"` when the column was empty — a guess about a row this
        method never saw written, and the same guess `fetchone()` used to make about
        which row it had found. A row with no recorded kind cannot reach here:
        `refusal_reason` refuses it first.
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
                row["memory_type"],
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
