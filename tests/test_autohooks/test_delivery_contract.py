"""The delivery contract of the autohooks daemon, driven through the REAL chain.

The daemon's promise is at-least-once delivery: a message that was not
written must not be lost. Three defects broke that promise, and none of them
could be seen from inside one component -- each one lived BETWEEN two
components that each behaved correctly on their own:

* A failed write was refused by the handler's own L0 guard. The capture had
  already committed, the write then failed, the row was closed as
  `gated_out`/`unaccounted_exit`, and the retry found that row and called it
  a duplicate. The message was never written and the cursor moved on.
* The transient boundary was a CLASS (`sqlite3.Error`) instead of a CODE, so
  a permanent `ProgrammingError` held the cursor forever -- a poison stall.
* A dispatch result that never reached a handler (the dedup middleware
  declines with `{"status": "deduped"}`) read as a delivery, so the cursor
  advanced past a message that had not been written.

Every test here drives the REAL chain: the real daemon, the real
`dispatch_event`, the real registry and the real `auto_save_text` on a real
(temporary) database. An injected `dispatch=` cannot reproduce any of these:
the injected callable never sees the swallowed exception, never sees the
middleware's decline, and never touches the journal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from autohooks.daemon import load_cursor, run_daemon, save_cursor
from autohooks.source import Message
from shared.connection import connection_manager
from shared.migrations import MigrationManager
from tests.test_autohooks.test_daemon import _cfg, _CursorSource

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# A message that is worth keeping: it scores above the importance gate and
# its atom is written to L3 (an OBSERVATION, not an invariant — the distiller
# routes observations to the episodic layer).
DELIVERABLE = "решение по архитектуре: переходим на PostgreSQL для основного проекта, миграция выполняется в ночное окне"


@pytest.fixture
async def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """A real, migrated database for the real chain to write into."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


def _register_real_user_hook() -> None:
    """Register the production `new_message` handler on the global registry."""
    from hooks.registry import hook_registry
    from hooks.user_hooks import UserHooks

    hook_registry.register_instance(UserHooks("u1"))


def _register_handler(handler: Any) -> None:
    """Register a stand-in handler for `new_message` on the global registry."""
    from hooks.registry import HookHandler, hook_registry

    hook_registry.register(
        HookHandler(
            func=handler,
            name="new_message",
            layer="user",
            is_async=True,
            takes_mem=False,
            takes_graph=False,
        )
    )


def _dedup_window(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Set the dedup middleware's window (the production value is 5 s)."""
    from shared.middleware import DedupMiddleware, default_pipeline

    for mw in default_pipeline._middlewares:
        if isinstance(mw, DedupMiddleware):
            monkeypatch.setattr(mw, "_window", seconds)


def _real_layer(cm: Any) -> tuple[Any, Any]:
    """A real memory manager and graph, on the given base."""
    from core import MemoryManager
    from graph.epistemic import EpistemicGraph

    return MemoryManager(cm=cm).get_layer("user", "u1"), EpistemicGraph(cm=cm, layer="user")


async def _episode_count(cm: Any) -> int:
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT count(*) FROM episodes")).fetchone()
    return int(row[0])


async def _journal_rows(cm: Any) -> list[dict[str, Any]]:
    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT id, status, decisions FROM l0_journal ORDER BY id")).fetchall()
    return [dict(r) for r in rows]


def _cfg_for(tmp_path: Path, user_id: str) -> Any:
    """The daemon config, with a user id of its own.

    The dedup middleware is a module-level singleton and its window map
    outlives a test, so two tests that dispatch the same payload under the
    same user id collide: the second one is declined before its first
    attempt. A per-test user id keeps them apart.
    """
    return replace(_cfg(tmp_path), user_id=user_id)


def _message(text: str, source_id: int = 1) -> Message:
    """One message from the source."""
    return Message(sender="user", text=text, ts=None, source_id=source_id)


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


def _sleeping_poll(seconds: float) -> Any:
    async def _poll(_requested: float) -> None:
        await asyncio.sleep(seconds)

    return _poll


# --------------------------------------------------------------------------
# IMPORTANT-1: the retry must be able to re-enter the write.
# --------------------------------------------------------------------------


async def test_a_message_whose_write_failed_transiently_is_written_on_the_retry(base: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole delivery contract, end to end.

    The L0 capture COMMITS before the write that fails. So the first attempt
    leaves a journal row behind and writes nothing; the retry then finds that
    row, and the handler's duplicate guard used to call it a duplicate and
    refuse it — forever. The daemon held the cursor, offered the message
    again, and the handler refused it again: the message was never written and
    the cursor never moved.

    The fix has to make three things true at once: the retry is not refused,
    the retry WRITES, and only then does the cursor advance.
    """
    import lifecycle.distiller as distiller

    real_distill = distiller.distill_and_route
    calls: list[int] = []

    async def _flaky(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            # The write phase loses the lock ONCE. The capture has already
            # committed, so the journal row exists while nothing was written.
            raise sqlite3.OperationalError("database is locked")
        return await real_distill(*args, **kwargs)

    monkeypatch.setattr(distiller, "distill_and_route", _flaky)
    # The dedup window would swallow the retry -- a different defect, covered
    # by its own test below. Here the retry must reach the handler.
    _dedup_window(monkeypatch, 0)
    _register_real_user_hook()

    mem, graph = _real_layer(base)
    src = _CursorSource([_message(DELIVERABLE)])
    save_cursor(tmp_path / "cursor.json", 0)

    # ── first attempt: the write fails, the cursor must stay behind the message
    await run_daemon(_cfg_for(tmp_path, "u-delivery"), src, mem=mem, graph=graph, rag=None, max_iterations=1, poll=_no_wait)

    assert calls == [1], "the write phase must have been attempted"
    assert load_cursor(tmp_path / "cursor.json") == 0, "the cursor must stay behind a message that was not written"
    assert await _episode_count(base) == 0, "the failed attempt must not have written anything"
    rows = await _journal_rows(base)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "gated_out", rows

    # ── the retry: the same message, offered again
    await run_daemon(_cfg_for(tmp_path, "u-delivery"), src, mem=mem, graph=graph, rag=None, max_iterations=1, poll=_no_wait)

    assert calls == [1, 1], "the failed write must be attempted again, not refused"
    assert await _episode_count(base) >= 1, "the retry must WRITE the memory"
    assert load_cursor(tmp_path / "cursor.json") == 1, "the cursor advances once the message was written"
    rows = await _journal_rows(base)
    assert rows[0]["status"] == "saved_l3", f"the journal row must carry the status of what really happened: {rows}"


async def test_an_unaccounted_exit_records_no_gate_decision(base: Any) -> None:
    """The close must not claim the gate decided this row.

    `features/replay.py` skips a row whose `decisions` already record the
    current (gate, config_hash) pair. That pair means "the gate refused this
    deliberately". An unaccounted exit is the opposite: the write never
    happened, so the pair would make the nightly replay skip the very row
    that still needs rescuing.
    """
    from shared.l0 import UNACCOUNTED_EXIT, capture, close_row

    rid = await capture("new_message", "user", "u1", DELIVERABLE)
    assert rid is not None
    await close_row(rid, "gated_out", reason=UNACCOUNTED_EXIT)

    rows = await _journal_rows(base)
    decisions = json.loads(rows[0]["decisions"] or "[]")
    assert not any(d.get("config_hash") for d in decisions), f"an unaccounted exit must record no gate decision: {decisions}"
    assert not any(d.get("gate") for d in decisions), f"an unaccounted exit must record no gate decision: {decisions}"


async def test_the_nightly_replay_still_picks_up_an_unaccounted_exit(base: Any) -> None:
    """The backstop must remain a backstop.

    The row is closed as `gated_out`, which the replay window selects, and it
    carries no (gate, config_hash) pair, so the replay must process it. If it
    skipped the row, the message would be stranded in the journal for good.
    """
    from features.replay import replay
    from shared.l0 import UNACCOUNTED_EXIT, capture, close_row

    rid = await capture("new_message", "user", "u1", DELIVERABLE)
    assert rid is not None
    await close_row(rid, "gated_out", reason=UNACCOUNTED_EXIT)

    result = await replay(since_days=60, respect_gate=False)
    assert result["processed"] == 1, f"the replay must rescue the row: {result}"
    assert result["skipped"] == 0, f"the replay must not skip the row: {result}"


# --------------------------------------------------------------------------
# IMPORTANT-2: the transient boundary is a CODE, not a class.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.ProgrammingError("no such column: text_preview"),
        sqlite3.IntegrityError("UNIQUE constraint failed: episodes.summary"),
        sqlite3.OperationalError("no such table: episodes"),
    ],
)
async def test_a_permanent_sqlite_failure_does_not_hold_the_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """A permanent failure must not stall the whole ingestion.

    `sqlite3.Error` also covers `ProgrammingError`, `IntegrityError` and a
    missing table. Holding the cursor for those is a poison stall: the daemon
    polls over one undeliverable message, silently, forever — the very
    failure the classification exists to prevent.
    """
    attempts: list[int] = []

    async def _handler(ctx: dict[str, Any]) -> dict[str, Any]:
        attempts.append(1)
        raise exc

    _register_handler(_handler)
    _dedup_window(monkeypatch, 0)

    src = _CursorSource([_message("a message worth keeping")])
    save_cursor(tmp_path / "cursor.json", 0)

    await run_daemon(_cfg_for(tmp_path, "u-permanent"), src, mem=None, graph=None, rag=None, max_iterations=3, poll=_no_wait)

    assert attempts == [1], f"a permanent failure must not be retried forever: {attempts}"
    assert load_cursor(tmp_path / "cursor.json") == 1, "the cursor must move past a message that can never be delivered"


async def test_a_permanent_sqlite_error_from_the_dispatch_is_logged_loudly(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The daemon's own catch must report a permanent failure at ERROR level.

    A busy database is a WARNING and the cursor waits. A permanent one is
    the opposite: the message is treated as handled (the cursor moves on) and
    the failure is logged loudly, because nothing else will report it again.
    """

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        raise sqlite3.ProgrammingError("no such table: episodes")

    src = _CursorSource([_message("a message worth keeping")])
    save_cursor(tmp_path / "cursor.json", 0)

    with caplog.at_level(logging.ERROR, logger="autohooks.daemon"):
        await run_daemon(
            _cfg_for(tmp_path, "u-permanent-dispatch"),
            src,
            mem=None,
            graph=None,
            rag=None,
            max_iterations=2,
            poll=_no_wait,
            dispatch=_dispatch,
        )

    assert load_cursor(tmp_path / "cursor.json") == 1, "a permanent failure must not hold the cursor"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "the permanent failure must be reported at ERROR level"
    assert "no such table: episodes" in errors[0], errors


async def test_a_busy_database_still_holds_the_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary must keep the case it was built for.

    SQLITE_BUSY (5) and SQLITE_BUSY_SNAPSHOT (517) mean "another writer
    holds the lock": the work was not done and doing it again is right.
    """
    attempts: list[int] = []

    async def _handler(ctx: dict[str, Any]) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"saved": True}

    _register_handler(_handler)
    _dedup_window(monkeypatch, 0)

    src = _CursorSource([_message("a message worth keeping")])
    save_cursor(tmp_path / "cursor.json", 0)

    await run_daemon(_cfg_for(tmp_path, "u-busy"), src, mem=None, graph=None, rag=None, max_iterations=2, poll=_no_wait)

    assert attempts == [1, 1], "a busy database must be retried"
    assert load_cursor(tmp_path / "cursor.json") == 1, "the cursor advances once the retry went through"


# --------------------------------------------------------------------------
# IMPORTANT-3: a result that never reached a handler is not a delivery.
# --------------------------------------------------------------------------


async def test_a_declined_event_is_retried_and_eventually_processed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A decline with no verdict must not be read as a delivery.

    The dedup middleware declines an event it has already seen within its
    window, and it says so with a shape that carries no `results` key: the
    event never reached a handler. Reading that as a delivery advanced the
    cursor past a message that had not been written — and the source is a
    chat log that cannot be re-read from the middle.

    The bound on the consequence: the decline is not a verdict on the EVENT
    (the same text is welcome again), it is a verdict on the MOMENT. So the
    message is offered again on the next tick and, once the window has
    passed, it is processed — the cursor is not stuck forever.
    """
    attempts: list[int] = []

    async def _handler(ctx: dict[str, Any]) -> dict[str, Any]:
        attempts.append(int(ctx.get("source_msg_id", -1)))
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"saved": True}

    _register_handler(_handler)
    # The production window is 5 s; a short one keeps the test quick while
    # still exercising the decline.
    _dedup_window(monkeypatch, 0.5)

    src = _CursorSource([_message("a message worth keeping")])
    save_cursor(tmp_path / "cursor.json", 0)

    await run_daemon(_cfg_for(tmp_path, "u-declined"), src, mem=None, graph=None, rag=None, max_iterations=8, poll=_sleeping_poll(0.1))

    assert attempts == [1, 1], f"the message must be delivered, not lost: {attempts}"
    assert load_cursor(tmp_path / "cursor.json") == 1, "the cursor must advance once the message went through"


async def test_a_declined_event_holds_the_cursor_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The decline must be VISIBLE.

    A silent decline is how the message was lost in the first place: nothing
    in the journal, nothing in the log, and a cursor that had moved on.
    """
    attempts: list[int] = []

    async def _handler(ctx: dict[str, Any]) -> dict[str, Any]:
        attempts.append(int(ctx.get("source_msg_id", -1)))
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"saved": True}

    _register_handler(_handler)
    _dedup_window(monkeypatch, 5)

    src = _CursorSource([_message("a message worth keeping")])
    save_cursor(tmp_path / "cursor.json", 0)

    with caplog.at_level(logging.WARNING, logger="autohooks.daemon"):
        await run_daemon(_cfg_for(tmp_path, "u-declined-hold"), src, mem=None, graph=None, rag=None, max_iterations=2, poll=_no_wait)

    assert attempts == [1], "the decline must not reach the handler"
    assert load_cursor(tmp_path / "cursor.json") == 0, "a message that was not delivered must keep the cursor behind it"
    warnings = [r.getMessage() for r in caplog.records if "never reached a handler" in r.getMessage()]
    assert warnings, f"the decline must be reported: {[r.getMessage() for r in caplog.records]}"
    assert "source_msg_id=1" in warnings[0], warnings[0]
