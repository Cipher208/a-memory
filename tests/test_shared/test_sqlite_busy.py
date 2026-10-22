"""The transient boundary of a SQLite failure, decided by CODE.

The daemon's at-least-once promise needs a boundary that is exactly as wide as
the failure it retries. `sqlite3.Error` is much wider: it also covers
`ProgrammingError`, `IntegrityError` and a missing table — failures that never
clear on their own. Retrying those holds the cursor forever, which is a poison
stall: the daemon polls over one undeliverable message, silently, forever.

The repository already had the correct boundary — `SQLITE_BUSY` (5) and
`SQLITE_BUSY_SNAPSHOT` (517) — as a private copy inside the write dispatcher.
This module is that boundary, named, so the registry (which classifies a
handler's failure) and the daemon (which acts on the classification) cannot
drift apart from it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from shared.sqlite_busy import SQLITE_BUSY, SQLITE_BUSY_SNAPSHOT, is_busy


def test_a_busy_database_is_transient() -> None:
    """SQLITE_BUSY: another writer holds the lock. It clears by itself."""
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = SQLITE_BUSY
    assert is_busy(exc) is True


def test_a_busy_snapshot_is_transient() -> None:
    """SQLITE_BUSY_SNAPSHOT: the read snapshot went stale and cannot be upgraded."""
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = SQLITE_BUSY_SNAPSHOT
    assert is_busy(exc) is True


def test_a_hand_built_busy_error_is_transient_by_its_text() -> None:
    """A hand-built error carries no code, so the text answers the question.

    `sqlite3.OperationalError("database is locked")` built in Python has no
    `sqlite_errorcode` attribute at all — the attribute is set by the C layer
    on a REAL error. The text fallback is what classifies it, and it is the
    behaviour the repository has always had, so it is pinned here.
    """
    exc = sqlite3.OperationalError("database is locked")
    assert not hasattr(exc, "sqlite_errorcode"), "the premise of this test"
    assert is_busy(exc) is True


def test_a_programming_error_is_permanent() -> None:
    """A missing column never clears. Retrying it stalls the ingestion."""
    exc = sqlite3.ProgrammingError("no such column: text_preview")
    assert is_busy(exc) is False


def test_an_integrity_error_is_permanent() -> None:
    """A broken uniqueness constraint never clears either."""
    exc = sqlite3.IntegrityError("UNIQUE constraint failed: episodes.summary")
    assert is_busy(exc) is False


def test_a_missing_table_is_permanent() -> None:
    """The table is missing. No amount of waiting brings it back."""
    exc = sqlite3.OperationalError("no such table: episodes")
    assert is_busy(exc) is False


def test_a_real_error_with_a_non_busy_code_is_permanent() -> None:
    """A real error carries its code, and the code decides."""
    exc = sqlite3.OperationalError("no such table: episodes")
    exc.sqlite_errorcode = 1  # SQLITE_ERROR
    assert is_busy(exc) is False


def test_a_non_sqlite_error_is_never_transient() -> None:
    """A handler bug is not a database moment."""
    assert is_busy(ValueError("a bug in the handler")) is False


async def test_the_registry_marks_a_busy_database_transient() -> None:
    """The registry is where the classification is produced, so it is where it is pinned."""
    from hooks.registry import HookHandler, HookRegistry

    async def _boom(ctx: dict[str, Any]) -> dict[str, Any]:
        raise sqlite3.OperationalError("database is locked")

    registry = HookRegistry()
    registry.register(
        HookHandler(
            func=_boom,
            name="new_message",
            layer="user",
            is_async=True,
            takes_mem=False,
            takes_graph=False,
        )
    )
    result = await registry.fire("new_message", "user", {"text": "x"})
    assert result["results"][0].get("transient") is True, result


async def test_the_registry_marks_a_permanent_sqlite_failure_permanent() -> None:
    """The defect this boundary exists for: a `sqlite3.Error` is not a busy moment."""
    from hooks.registry import HookHandler, HookRegistry

    async def _boom(ctx: dict[str, Any]) -> dict[str, Any]:
        raise sqlite3.ProgrammingError("no such column: text_preview")

    registry = HookRegistry()
    registry.register(
        HookHandler(
            func=_boom,
            name="new_message",
            layer="user",
            is_async=True,
            takes_mem=False,
            takes_graph=False,
        )
    )
    result = await registry.fire("new_message", "user", {"text": "x"})
    assert result["results"][0].get("transient") is False, result


def test_there_is_one_boundary_and_not_two_copies() -> None:
    """The write dispatcher must call the shared helper, not keep its own copy.

    Two copies of the same question are two answers waiting to diverge: the
    dispatcher would report a busy write as busy while the registry called the
    same error permanent.
    """
    from shared import sqlite_busy, write_dispatch

    assert write_dispatch.is_busy is sqlite_busy.is_busy
