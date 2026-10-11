"""ArchivedMemories — async archived memory storage."""

from __future__ import annotations
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

#: Archive reasons that record a *deliberate* removal rather than a routine one.
#:
#: A row carrying one of these is not restorable through the primitive — ever,
#: not even when the caller knows both its id and its kind. Only `archive_reason`
#: can tell a forgotten fact from a removed one, and a bin that hands back what
#: was deliberately removed makes the removal decorative.
#:
#: `foreign_agent_contamination` is written by `scripts/purge_foreign_rows.py`,
#: which archives before it removes. On the base measured 2026-10-11 that was 247
#: of 606 archived rows (41%) — 238 episodes and 9 core entries. If they ever need
#: to come back, that is an operator script run by hand, not a tool an agent can
#: call — which is why nothing here, and nothing in the restore path, has a
#: parameter that turns the refusal off.
PURGE_REASONS = frozenset({"foreign_agent_contamination"})


def is_restorable_reason(reason: str | None) -> bool:
    """Whether a row carrying this reason may be handed back to a caller.

    The listing and the restore both ask this one function, so the bin can never
    advertise a row that the restore would then refuse.
    """
    return (reason or "") not in PURGE_REASONS


class ArchivedMemories:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager
        self._ready = False

    async def ensure(self) -> None:
        """Create the table if it is not there yet, once per instance.

        Same shape as `DreamBuffer.ensure`, and for the same reason: this class
        is constructed in several places that never run migrations, so a method
        that touches the table has to be able to bring it into existence.

        Why it was needed: `archive()` and `get_archived()` used to assume the
        table existed. On a database that had run the migration it did, which
        hid the problem — `tests/test_shared/test_shared.py::test_archived_memories`
        passed only when another test had migrated the shared session database
        first. Run alone, or with `-p no:randomly`, it failed with
        `no such table: archived_memories`. A test that needs a neighbour to
        pass is not testing the class.
        """
        if self._ready:
            return
        conn = await self._cm.get(DB_NAME)
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(archived_memories)")).fetchall()]
        if not cols:
            await self._init_db()
        self._ready = True

    async def _init_db(self) -> None:
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS archived_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                original_id INTEGER, content TEXT NOT NULL,
                memory_type TEXT, importance REAL,
                archive_reason TEXT NOT NULL,
                archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_archived_user ON archived_memories(user_id, archived_at DESC);
        """,
        )

    async def archive(
        self,
        user_id: str,
        content: str,
        memory_type: str | None = None,
        importance: float | None = None,
        original_id: int | None = None,
        reason: str = "manual",
    ) -> int:
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, original_id, content, memory_type, importance, reason),
        )
        await conn.commit()
        last_id: Any = cursor.lastrowid
        return int(last_id) if last_id is not None else 0

    async def get_archived(self, user_id: str = "default", limit: int = 50) -> dict[str, Any]:
        """Return the bin as an agent may see it: restorable rows, and a count of the rest.

        WHY THIS RETURNS A DICT AND NOT A LIST. A listing that silently omits
        rows is worse than a short one, because a bin that looks emptier than it
        is breeds false confidence. So the purge rows are filtered out of
        `entries` and their number is **declared** in `omitted`, with the reason
        in `omitted_reason`. The arithmetic is part of the answer, not a
        footnote: `shown + omitted` is everything this user has.

        `shown` counts the page; `omitted` counts every hidden row for this user,
        however many pages that would take. The two are different scopes on
        purpose — a page size is not a fact about the bin.

        **Hidden is not deleted.** The purge rows stay in the database; hiding is
        a view, and hiding is exactly how a thing gets forgotten, which is why
        the count is not allowed to go quiet.

        Each entry carries `memory_type` and `original_id` beside the content:
        without them a caller cannot name the row it wants back, and an
        unnameable bin is unusable rather than merely full.
        """
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        hidden = sorted(PURGE_REASONS)
        marks = ",".join(["?"] * len(hidden))
        cursor = await conn.execute(
            f"SELECT * FROM archived_memories WHERE user_id=? AND archive_reason NOT IN ({marks}) ORDER BY archived_at DESC LIMIT ?",
            (user_id, *hidden, limit),
        )
        rows = await cursor.fetchall()
        count_row = await (
            await conn.execute(
                f"SELECT COUNT(*) FROM archived_memories WHERE user_id=? AND archive_reason IN ({marks})",
                (user_id, *hidden),
            )
        ).fetchone()
        omitted = int(count_row[0]) if count_row else 0
        return {
            "entries": [
                {
                    "id": r["id"],
                    "content": r["content"],
                    "importance": r["importance"],
                    "memory_type": r["memory_type"],
                    "original_id": r["original_id"],
                    "archive_reason": r["archive_reason"],
                    "archived_at": r["archived_at"],
                }
                for r in rows
            ],
            "shown": len(rows),
            "omitted": omitted,
            # None rather than a reason when nothing was hidden: naming a reason
            # for an omission that did not happen is its own small lie.
            "omitted_reason": "purge" if omitted else None,
        }

    async def count(self, user_id: str = "default") -> int:
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM archived_memories WHERE user_id=?", (user_id,))).fetchone()
        return int(row[0]) if row else 0
