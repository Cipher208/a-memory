"""The importance sweep must survive a competing writer.

Measured on the live hermes base, 21.10.2026: the 30-minute sweep lost its whole
pass whenever another writer held the SQLite write lock. The sweep writes on its
own raw connection on purpose (#132), so it sits outside the process's
single-writer discipline: its `UPDATE core_memory SET importance=...` failed with
`OperationalError: database is locked` once the sweep's own busy timeout (15 s)
had elapsed, the failure was counted, and the pass was abandoned. With a single
user in the base, one collision cost every row of the pass and the next attempt
was half an hour away. The same thing happened 14 times on 17.-18.10.

The failure was never classified either: the log said "database is locked" and
nothing about which SQLite error it was - 5 (`SQLITE_BUSY`, another connection
holds the write lock) or 517 (`SQLITE_BUSY_SNAPSHOT`, this connection's read
snapshot went stale). The two want different explanations and only one of them
is a competing writer, so the next occurrence had to be guessed at.

These tests pin the shapes of the fix: the sweep reconnects and retries the
user after a busy failure, the retries are bounded per user AND per pass, and
when they run out the failure says which SQLite error it was and how many
attempts it spent.
"""

import contextlib
import logging
import re
import sqlite3
import threading
import time

import pytest

from lifecycle.importance_scheduler import ImportanceScheduler, SchedulerConfig
from shared.connection import AsyncConnectionManager

SCHEMA = """
CREATE TABLE core_memory (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT, "key" TEXT, value TEXT,
    importance REAL, memory_kind TEXT,
    updated_at REAL, created_at REAL
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    user_id TEXT, layer TEXT, action TEXT,
    target_id TEXT, details TEXT, timestamp REAL
);
CREATE TABLE importance_audit (
    id INTEGER PRIMARY KEY,
    user_id TEXT, chunk_id INTEGER, source TEXT,
    old_importance REAL, new_importance REAL,
    signal_breakdown TEXT, reason TEXT, rescored_at REAL
);
"""


@pytest.fixture
async def cm(tmp_path):
    m = AsyncConnectionManager(base_dir=str(tmp_path))
    await m.execute_script("memory.db", SCHEMA)
    return m


async def _seed(cm, user_id: str, value: str = "redis cluster pipelining") -> None:
    """One row the sweep must rescore: its score moves away from 0.4."""
    conn = await cm.get("memory.db")
    cur = await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, "k", value, 0.4, time.time(), time.time()),
    )
    await cur.close()
    await conn.commit()


class _CompetingWriter:
    """A raw connection on the same base that holds the SQLite write lock.

    The production contender was another process on the same base (the autohooks
    daemon), which takes the write lock with `BEGIN IMMEDIATE` and holds it while
    it writes. The sweep's connection is a different one, so this is the exact
    production shape: a foreign writer holding the base's write lock while the
    sweep tries to write on it.

    The lock is held until the test releases it. A self-releasing timer would let
    the sweep's first write succeed if the sweep reached its UPDATE late, and the
    test would then pass without ever having exercised the retry.
    """

    def __init__(self, db_path, safety_seconds: float = 5.0) -> None:
        self._db_path = db_path
        self._safety_seconds = safety_seconds
        self._release = threading.Event()
        self._held = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="competing-writer")
        self._thread.start()
        assert self._held.wait(5.0), "the competing writer never took the write lock"

    def _run(self) -> None:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._held.set()
            # The safety net only exists so a broken sweep cannot hang the test:
            # the lock goes when the test releases it.
            self._release.wait(self._safety_seconds)
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            self._held.set()
        finally:
            conn.close()

    def release(self) -> None:
        """Let the writer give the lock back."""
        self._release.set()

    def stop(self) -> None:
        self.release()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def _give_up_lines(caplog) -> list[str]:
    """The log lines where the sweep gave up on a user.

    The warning per retry names the SQLite error too, so the lines that also say
    "failed" are picked out explicitly.
    """
    return [r.getMessage() for r in caplog.records if "failed" in r.getMessage() and "sqlite error" in r.getMessage()]


@pytest.mark.asyncio
async def test_the_sweep_survives_a_competing_writer(cm, monkeypatch, caplog):
    """The production failure: one busy write must not cost the whole pass.

    A foreign connection holds the write lock, the sweep's write fails, and the
    sweep must reconnect and rescore the same user anyway. Before the fix the
    pass was lost: `errors=1`, `rescored=0`, and half an hour until the next try.

    The retry is asserted, not assumed: the lock is released only after the
    first attempt has failed, so the first attempt cannot succeed - and the call
    count says whether the retry happened at all.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    monkeypatch.setattr("lifecycle.importance_scheduler._SWEEP_TIMEOUT_S", 0.2)
    await _seed(cm, "alice")

    writer = _CompetingWriter(cm.base_dir / "memory.db")
    writer.start()

    calls: list[str] = []
    real_rescore = ImportanceScheduler._rescore_user

    def watched(self, user_id, conn, stats):
        calls.append(user_id)
        try:
            return real_rescore(self, user_id, conn, stats)
        except sqlite3.OperationalError:
            # The first attempt has failed and the lock can go now, so the retry
            # finds it free.
            writer.release()
            raise

    monkeypatch.setattr(ImportanceScheduler, "_rescore_user", watched)

    try:
        s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
        with caplog.at_level(logging.INFO):
            stats = await s.run_once()
    finally:
        writer.stop()

    assert stats["errors"] == 0, stats
    assert stats["rescored"] >= 1, stats
    # Two calls: the attempt that lost the lock and the retry that won it. A pass
    # that never lost the lock made one call, and this test would then have
    # passed on a sweep without a retry at all.
    assert len(calls) == 2, f"the sweep must retry the user after the busy failure: {calls}"
    assert "reconnecting and retrying" in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_the_sweep_reports_the_sqlite_code_when_the_retries_run_out(cm, monkeypatch, caplog):
    """A writer that never releases the lock must leave a diagnosable trace.

    The error is counted once, not once per attempt, and the log says which
    SQLite error it was - the whole point of the classification. Without it the
    next occurrence is a guess between "another writer held the lock" and "this
    connection's snapshot went stale".
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    monkeypatch.setattr("lifecycle.importance_scheduler._SWEEP_TIMEOUT_S", 0.1)
    await _seed(cm, "alice")

    writer = _CompetingWriter(cm.base_dir / "memory.db")
    writer.start()
    try:
        s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
        with caplog.at_level(logging.INFO):
            stats = await s.run_once()
    finally:
        writer.stop()

    assert stats["errors"] == 1, stats
    assert stats["rescored"] == 0, stats

    given_up = _give_up_lines(caplog)
    assert len(given_up) == 1, f"the failure must be logged once: {caplog.text!r}"
    message = given_up[0]

    code = re.search(r"sqlite error (\d+)", message)
    assert code is not None, f"the failure must name its SQLite error: {message!r}"
    assert int(code.group(1)) in (5, 517), message


@pytest.mark.asyncio
async def test_the_sweep_stops_when_the_pass_wide_budget_is_spent(cm, monkeypatch, caplog):
    """The pass may not spend more than about two busy timeouts in total.

    The per-user retry bound alone lets a base with N blocked users hold the
    maintenance lock for N x 3 x busy timeouts, and that lock is what excludes the
    15-minute cleanup and the nightly pass. The budget is per pass, so the cost
    of a contended base does not grow with the number of users in it.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    monkeypatch.setattr("lifecycle.importance_scheduler._SWEEP_TIMEOUT_S", 0.1)
    for user in ("alice", "bob", "carol"):
        await _seed(cm, user)

    writer = _CompetingWriter(cm.base_dir / "memory.db")
    writer.start()

    calls: list[str] = []
    real_rescore = ImportanceScheduler._rescore_user

    def watched(self, user_id, conn, stats):
        calls.append(user_id)
        return real_rescore(self, user_id, conn, stats)

    monkeypatch.setattr(ImportanceScheduler, "_rescore_user", watched)

    try:
        s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
        with caplog.at_level(logging.INFO):
            stats = await s.run_once()
    finally:
        writer.stop()

    # The budget is two busy timeouts: the first lost write charges one, the
    # second spends the budget, and the pass stops there. The other two users are
    # not attempted at all - a retry per user would have been three attempts each.
    assert len(calls) == 2, f"the pass must stop when the budget is spent: {calls}"
    assert stats["errors"] == 1, stats
    assert "budget" in caplog.text, caplog.text
    assert "not attempted" in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_the_sweep_gives_up_on_a_user_after_the_per_user_bound(cm, monkeypatch, caplog):
    """An instant busy failure spends no waiting, so the per-user bound is what stops it.

    A stale snapshot (517) fails without consulting the busy handler, so it costs
    no time under the lock and does not spend the pass-wide budget. What bounds
    the user is the per-user retry count.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    await _seed(cm, "alice")

    def boom(uid, conn, stats):
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = 517
        exc.sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"
        raise exc

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    monkeypatch.setattr(s, "_rescore_user", boom)

    with caplog.at_level(logging.INFO):
        stats = await s.run_once()

    assert stats["errors"] == 1, stats
    given_up = _give_up_lines(caplog)
    assert len(given_up) == 1, f"the failure must be logged once: {caplog.text!r}"
    assert "after 3 attempts" in given_up[0], given_up[0]


@pytest.mark.asyncio
async def test_an_unclassified_sqlite_error_is_logged_as_unknown(cm, monkeypatch, caplog):
    """An exception without a SQLite error code must not print "None".

    SQLite sets `sqlite_errorcode` on the errors it raises itself; an exception
    built by hand has no such attribute, and the log must say so in words.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    await _seed(cm, "alice")

    def boom(uid, conn, stats):
        raise sqlite3.OperationalError("database is locked")

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    monkeypatch.setattr(s, "_rescore_user", boom)

    with caplog.at_level(logging.INFO):
        stats = await s.run_once()

    assert stats["errors"] == 1, stats
    assert "sqlite error unknown" in caplog.text, caplog.text
    assert "sqlite error None" not in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_a_failed_reconnect_does_not_abort_the_pass(cm, monkeypatch, caplog):
    """A replacement connection that cannot be opened must cost one user, not the pass.

    Opening the replacement happens inside the error handler, so an exception
    there would escape the pass: no summary, and the users that were already
    rescored would go unreported.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    monkeypatch.setattr("lifecycle.importance_scheduler._SWEEP_TIMEOUT_S", 0.1)
    await _seed(cm, "alice")

    writer = _CompetingWriter(cm.base_dir / "memory.db")
    writer.start()

    real_open = ImportanceScheduler._open_sweep_connection
    opened: list[object] = []

    def flaky(self, db_path):
        opened.append(db_path)
        if len(opened) > 1:
            raise OSError("no space left on device")
        return real_open(self, db_path)

    monkeypatch.setattr(ImportanceScheduler, "_open_sweep_connection", flaky)

    try:
        s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
        with caplog.at_level(logging.INFO):
            stats = await s.run_once()
    finally:
        writer.stop()

    assert stats["errors"] == 1, stats
    assert "replacement connection" in caplog.text, caplog.text
    # The pass still reported what it did.
    assert "importance sweep" in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_the_sweep_without_a_competitor_is_unchanged(cm, monkeypatch):
    """No competitor, no retry: the normal flow must not change.

    The retry path is a new branch around the write; the pass without a
    competing writer must still rescore and still report no errors.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    await _seed(cm, "alice")

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    stats = await s.run_once()

    assert stats["errors"] == 0, stats
    assert stats["rescored"] >= 1, stats
