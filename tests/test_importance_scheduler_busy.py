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

These tests pin the two shapes of the fix: the sweep reconnects and retries the
user after a busy failure, and when the retries are exhausted it says which
SQLite error it was and how many attempts it spent.
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


async def _seed_one_rescorable(cm):
    """One row the sweep must rescore: its score moves away from 0.4."""
    conn = await cm.get("memory.db")
    cur = await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("alice", "k", "redis cluster pipelining", 0.4, time.time(), time.time()),
    )
    await cur.close()
    await conn.commit()
    return conn


class _CompetingWriter:
    """A raw connection on the same base that holds the SQLite write lock.

    The production contender was another process on the same base (the autohooks
    daemon), which takes the write lock with `BEGIN IMMEDIATE` and holds it while
    it writes. The sweep's connection is a different one, so this is the exact
    production shape: a foreign writer holding the base's write lock while the
    sweep tries to write on it.
    """

    def __init__(self, db_path, hold_seconds: float = 0.3) -> None:
        self._db_path = db_path
        self._hold_seconds = hold_seconds
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
            self._release.wait(self._hold_seconds)
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


@pytest.mark.asyncio
async def test_the_sweep_survives_a_competing_writer(cm, monkeypatch):
    """The production failure: one busy write must not cost the whole pass.

    A foreign connection holds the write lock, the sweep's write fails, and the
    sweep must reconnect and rescore the same user anyway. Before the fix the
    pass was lost: `errors=1`, `rescored=0`, and half an hour until the next try.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    monkeypatch.setattr("lifecycle.importance_scheduler._SWEEP_TIMEOUT_S", 0.2)
    await _seed_one_rescorable(cm)

    writer = _CompetingWriter(cm.base_dir / "memory.db", hold_seconds=0.3)
    writer.start()

    # Release the lock as soon as the sweep's first attempt has failed, so the
    # retry finds it free. The fixed release time stays the safety net: the
    # sweep has to survive a lock that is held across its whole busy timeout.
    real_rescore = ImportanceScheduler._rescore_user

    def watched(self, user_id, conn, stats):
        try:
            return real_rescore(self, user_id, conn, stats)
        except sqlite3.OperationalError:
            writer.release()
            raise

    monkeypatch.setattr(ImportanceScheduler, "_rescore_user", watched)

    try:
        s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
        stats = await s.run_once()
    finally:
        writer.stop()

    assert stats["errors"] == 0, stats
    assert stats["rescored"] >= 1, stats


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
    await _seed_one_rescorable(cm)

    writer = _CompetingWriter(cm.base_dir / "memory.db", hold_seconds=30.0)
    writer.start()
    try:
        s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
        with caplog.at_level(logging.INFO):
            stats = await s.run_once()
    finally:
        writer.stop()

    assert stats["errors"] == 1, stats
    assert stats["rescored"] == 0, stats

    # The line that gives up is the one that has to carry the diagnosis: which
    # SQLite error it was, and how many attempts it spent on it. The warning
    # per retry also names the error, so the exhausted failure is picked out
    # explicitly instead of matching the first "sqlite error" in the log.
    given_up = [r.getMessage() for r in caplog.records if "failed after" in r.getMessage()]
    assert given_up, f"the exhausted failure must be logged: {caplog.text!r}"
    message = given_up[0]

    code = re.search(r"sqlite error (\d+)", message)
    assert code is not None, f"the failure must name its SQLite error: {message!r}"
    assert int(code.group(1)) in (5, 517), message

    attempts = re.search(r"after (\d+) attempts", message)
    assert attempts is not None, f"the failure must say how many attempts it spent: {message!r}"
    assert int(attempts.group(1)) == 3, message


@pytest.mark.asyncio
async def test_the_sweep_without_a_competitor_is_unchanged(cm, monkeypatch):
    """No competitor, no retry: the normal flow must not change.

    The retry path is a new branch around the write; the pass without a
    competing writer must still rescore and still report no errors.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    await _seed_one_rescorable(cm)

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    stats = await s.run_once()

    assert stats["errors"] == 0, stats
    assert stats["rescored"] >= 1, stats
