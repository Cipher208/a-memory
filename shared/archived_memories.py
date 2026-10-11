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

#: Where a row must go back to, named by its `memory_type`.
#:
#: WHY THIS IS A DENYLIST AND NOT AN ALLOWLIST. An allowlist was tried first —
#: `frozenset(MemoryKind)` — and it was wrong, because it assumed the column is
#: constrained. It is not: `core_memory.memory_kind` is a bare `TEXT` with no
#: CHECK, one write path coerces through `MemoryKind` and the column never does,
#: and the live bases hold kinds the enum has never heard of — `log` (1 row) and
#: `bug` (3 rows), measured 2026-10-11. An enum-shaped allowlist therefore refuses
#: rows that legitimately came out of `core_memory`, and a false refusal is worse
#: than the defect it replaces: it reads like a rule. What can be *named* here is
#: the small, closed set of sources that are not `core_memory`; what can be a core
#: kind is open, so that side is the default rather than a list.
ROUTE_EPISODES = "episodes"
ROUTE_CORE = "core_memory"

#: Archived for the audit trail, with no restore implementation behind them. Named
#: so their rows are refused rather than written into `core_memory` as though they
#: had lived there: `graph:<node_type>` is written by `forget`'s shadow bin for a
#: graph node, and `project_archive` for a **wiki page** (`project.py` deletes the
#: file). Neither has a restore path in this repository, so the honest answer is
#: that there is nowhere to put them.
#:
#: `core_memory` is deliberately **not** in this set. It was, on a story that turned
#: out to be invented — that the value was a table name which had leaked into a kind
#: field. It has not: `scripts/purge_foreign_rows.py:209` writes it for rows it
#: selected **from** `core_memory`, so it names the source, exactly as `episode`
#: names `episodes`. See `route_of`.
_NO_ROUTE_KINDS = frozenset({"project_archive"})
_NO_ROUTE_PREFIXES = ("graph:",)

#: The reasons a **row's reason or kind** can put it out of reach. These are the two
#: the bin counts in `omitted_by_reason`, and between them they are complete for that
#: question.
#:
#: They are not the only refusals a restore can return. `ambiguous` is a third, and it
#: is decided in `ForgettingSystem.restore_entries` because it depends on the *call*
#: — an id naming two rows of different kinds for one owner is refused when no kind is
#: given and accepted when it is. `not_found` is a fourth and is not a refusal at all,
#: just an id that names nothing. So a listing that hides on these two reasons can
#: still be asked for a row that the restore then refuses, and the difference is the
#: kind the listing returns — see the anti-drift test.
REFUSAL_PURGED = "purged"
REFUSAL_UNROUTABLE = "unroutable"
#: Unlike the two above, this one is not a property of the row: the same row is returned
#: willingly into an empty table and refused when its key is already there. It is here so
#: that the vocabulary of refusals has one home, and because the alternative — letting the
#: `UNIQUE` index raise — takes the whole batch down with one row and reports nothing at
#: all, which is the silence this module was repaired to end.
REFUSAL_ALREADY_PRESENT = "already_present"
#: The call itself broke. Listed here rather than invented at the raise, so a caller
#: switching on the reason has one place to learn the vocabulary. `original_id` may be
#: `None` on this one: the failure can happen with no row in flight.
REFUSAL_FAILED = "error"


def is_restorable_reason(reason: str | None) -> bool:
    """Whether a row carrying this reason may be handed back to a caller."""
    return (reason or "") not in PURGE_REASONS


def route_of(memory_type: str | None) -> str | None:
    """Where a row carrying this kind goes back to, or `None` if nowhere.

    The convention is that the value names the table the row came from: `episode`
    for `episodes` (the name is singular, the table is not), `core_memory` for
    `core_memory` — which is what `scripts/purge_foreign_rows.py:209` writes — and
    an L4 kind such as `fact` or `rule` for a row from `core_memory`, where the kind
    is the more useful thing to know. So `episodes` is named explicitly and
    `core_memory` is the default, with a small closed set of labels that name no
    table this can write to (`_NO_ROUTE_KINDS`, `_NO_ROUTE_PREFIXES`).

    `None` is answered in two different situations on purpose — a kind that was never
    recorded, and a label with no restore path — because both mean the same thing to
    a caller: this row cannot be handed back, and writing it into `core_memory` would
    be inventing a provenance it never had.
    """
    kind = (memory_type or "").strip()
    if not kind:
        # The writer did not say. **One** caller in this repository leaves it empty:
        # `rag/conflict.py:177` archives rows out of `memory_conflicts`, which are not
        # core rows and have no table of their own to return to. Filling in a default
        # here is the same guess `fetchone()` used to make about which row it had
        # found, so it is refused instead.
        return None
    if kind == "episode":
        return ROUTE_EPISODES
    if kind in _NO_ROUTE_KINDS or kind.startswith(_NO_ROUTE_PREFIXES):
        return None
    return ROUTE_CORE
    if kind == "episode":
        return ROUTE_EPISODES
    if kind in _NO_ROUTE_KINDS or kind.startswith(_NO_ROUTE_PREFIXES):
        return None
    return ROUTE_CORE


def refusal_reason(reason: str | None, memory_type: str | None) -> str | None:
    """Why this row's reason or kind puts it out of reach, or `None` if neither does.

    The listing and the restore both ask this one function, in this one order, so
    they cannot drift apart about a reason or a kind. The order is part of the
    answer: a purge reason is refused whatever the row claims to be, which is what
    keeps a deliberately removed row out of reach even when a caller names its id
    and its kind.

    **What this does not decide.** A `None` here does not mean the restore will
    certainly accept the row. It means this row's reason and kind are not themselves
    a bar — `ambiguous` and `not_found` depend on the call, not the row, and are
    returned by `restore_entries`. That is also why the listing can offer an id the
    restore would refuse *in that call*: the `memory_type` it returns is the argument
    which resolves it.
    """
    if not is_restorable_reason(reason):
        return REFUSAL_PURGED
    if route_of(memory_type) is None:
        return REFUSAL_UNROUTABLE
    return None


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
        """Record a row in the bin.

        `memory_type` is the **only** channel through which a later restore can
        learn where a row came from, and nothing here enforces it: the column is a
        bare `TEXT`, this parameter is free text, and a caller that omits it makes
        the row unrestorable in place — `route_of` refuses such a row rather than
        guessing a destination.

        So the two ends are not the same expression, even though they look alike.
        Two of this method's callers pass `memory_kind or "fact"`, and on the
        **writing** side that is legitimate: the row was just read out of
        `core_memory`, so naming it a core kind records where it came from. The same
        `or "fact"` on the **reading** side is the defect `route_of` exists to
        prevent — there the origin is unknown, and a default is a guess about a table
        the reader never saw the row leave. (The string also appears elsewhere in the
        repository — `core/memory.py`, `hooks/shared.py`, `importance_scheduler.py` —
        but those are not calls to this method and were counted as though they were
        in an earlier version of this docstring.)
        """
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
        is breeds false confidence. So the rows the restore would refuse are
        filtered out of `entries` and their number is **declared**, broken down by
        the rule that hid them, in `omitted_by_reason`.

        THE ARITHMETIC, AND WHICH ONE IS TRUE. `restorable + omitted` is every row
        this user has. `shown + omitted` is **not** — `shown` is one page — and an
        earlier version of this docstring claimed it was, which is a statement that
        holds only while the bin is smaller than the page. So the two scopes are
        kept apart and both are returned: `shown` is the page, `restorable` is the
        bin's count of rows a restore would act on, `omitted` is the bin's count of
        rows it would not.

        **Hidden is not deleted.** Those rows stay in the database; hiding is a
        view, and hiding is exactly how a thing gets forgotten, which is why the
        count is not allowed to go quiet. `omitted_by_reason` carries its zero keys
        rather than dropping them, so an empty bin and a bin whose reasons were
        never computed do not look alike.

        WHICH ROWS ARE HIDDEN, AND WHICH ARE NOT. A purge reason, and a kind with
        no route — both decided by `refusal_reason`, the same function the restore
        asks, so the listing cannot drift from it. An id naming **two** rows of
        different kinds is deliberately **shown**: the listing returns
        `memory_type`, and that is exactly the argument that resolves it, so
        hiding it would throw away a row that can be had.

        Each entry carries `memory_type` and `original_id` beside the content:
        without them a caller cannot name the row it wants back, and an
        unnameable bin is unusable rather than merely full.
        """
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        # Every row for this user, classified in Python by the same function the
        # restore uses. A `WHERE` clause would be faster and would duplicate the
        # rules in a second language — which is the drift this is built to avoid —
        # and the counts below need every row anyway. `content` is deliberately not
        # read here: rows that will be hidden never have their text loaded.
        cursor = await conn.execute(
            "SELECT id, original_id, memory_type, importance, archive_reason, archived_at "
            "FROM archived_memories WHERE user_id=? ORDER BY archived_at DESC, id DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()

        kept: list[Any] = []
        omitted_by_reason = {REFUSAL_PURGED: 0, REFUSAL_UNROUTABLE: 0}
        for row in rows:
            why = refusal_reason(row["archive_reason"], row["memory_type"])
            if why is None:
                kept.append(row)
            else:
                omitted_by_reason[why] += 1

        page = kept[:limit]
        contents: dict[int, Any] = {}
        if page:
            marks = ",".join(["?"] * len(page))
            fetched = await (
                await conn.execute(
                    f"SELECT id, content FROM archived_memories WHERE id IN ({marks})",
                    tuple(int(r["id"]) for r in page),
                )
            ).fetchall()
            contents = {int(r["id"]): r["content"] for r in fetched}

        return {
            "entries": [
                {
                    "id": r["id"],
                    "content": contents.get(int(r["id"])),
                    "importance": r["importance"],
                    "memory_type": r["memory_type"],
                    "original_id": r["original_id"],
                    "archive_reason": r["archive_reason"],
                    "archived_at": r["archived_at"],
                }
                for r in page
            ],
            "shown": len(page),
            "restorable": len(kept),
            "omitted": sum(omitted_by_reason.values()),
            "omitted_by_reason": omitted_by_reason,
        }

    async def count(self, user_id: str = "default") -> int:
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM archived_memories WHERE user_id=?", (user_id,))).fetchone()
        return int(row[0]) if row else 0
