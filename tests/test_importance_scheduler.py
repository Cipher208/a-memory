"""Tests for importance scheduler."""

import asyncio
import logging
import sqlite3
import time

import pytest

from lifecycle.importance_scheduler import ImportanceScheduler, SchedulerConfig
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def cm(tmp_path):
    m = AsyncConnectionManager(base_dir=str(tmp_path))
    await m.execute_script(
        "memory.db",
        """
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
        """,
    )
    return m


@pytest.mark.asyncio
async def test_scheduler_rescores_big_delta(cm, monkeypatch):
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("alice", "k", "redis cluster pipelining", 0.4, time.time(), time.time()),
    )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    stats = await s.run_once()
    assert stats["rescored"] >= 1

    row = await (await conn.execute("SELECT importance FROM core_memory WHERE \"key\"='k'")).fetchone()
    # Score should change (0.4 → new score based on tech keywords + retrieval baseline)
    assert row["importance"] != 0.4

    audit = await (await conn.execute("SELECT reason FROM importance_audit ORDER BY id DESC LIMIT 1")).fetchone()
    assert audit["reason"] == "scheduled"


@pytest.mark.asyncio
async def test_scheduler_skips_small_delta(cm, monkeypatch):
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, memory_kind, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("alice", "rule1", "важная инструкция по безопасности", 0.95, "rule", time.time(), time.time()),
    )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=1.0))
    stats = await s.run_once()
    # With delta_threshold=1.0, the change (0.95→0.4) is within threshold
    assert stats["skipped"] >= 1


@pytest.mark.asyncio
async def test_scheduler_retrieval_signal_boosts(cm, monkeypatch):
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("alice", "useful", "redis config notes", 0.5, time.time(), time.time()),
    )
    for _ in range(20):
        await conn.execute(
            """INSERT INTO audit_log
               (user_id, layer, action, target_id, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            ("alice", "core_memory", "recall_useful", "1", time.time()),
        )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    await s.run_once()
    row = await (await conn.execute("SELECT importance FROM core_memory WHERE \"key\"='useful'")).fetchone()
    # Score should change from 0.5 due to retrieval signal
    assert row["importance"] != 0.5


@pytest.mark.asyncio
async def test_scheduler_ignores_old(cm, monkeypatch):
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    long_ago = time.time() - 365 * 86400
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("alice", "old", "redis", 0.3, long_ago, long_ago),
    )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(only_recent_days=30))
    stats = await s.run_once()
    assert stats["rescored"] == 0


@pytest.mark.asyncio
async def test_scheduler_spares_curated_sources(cm, monkeypatch):
    """Authored entries must not be steamrolled by content heuristics (E1 18.09)."""
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    await conn.execute("ALTER TABLE core_memory ADD COLUMN source TEXT DEFAULT ''")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, memory_kind, source, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("alice", "rule:speak-first", "speak first on noise", 0.95, "rule", "calibration", time.time(), time.time()),
    )
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, memory_kind, source, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("alice", "fact:auto", "redis cluster pipelining", 0.4, "fact", "episode_promotion", time.time(), time.time()),
    )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.0))
    await s.run_once()

    kept = await (await conn.execute("SELECT importance FROM core_memory WHERE \"key\"='rule:speak-first'")).fetchone()
    assert kept["importance"] == 0.95


# --- surviving a competing writer -------------------------------------------------
#
# Measured on the live base 16.10.2026: the sweep failed on `UPDATE core_memory` with
# `OperationalError: database is locked`, on nearly every pass, every 30 minutes, for
# four days — while a second process served the same base. Two shapes made that fatal,
# and both are pinned below: a read cursor held open across the writes, and no retry
# when a write loses the race.


class _WriteBlockedWhileACursorIsOpen:
    """Models the live window: a write during an open read statement gets BUSY.

    On a WAL database SQLite cannot upgrade a read snapshot to a write once another
    connection has committed, and it answers BUSY *without* consulting `busy_timeout`.
    A cursor still returning rows is exactly that state, so this wrapper raises the
    same error the live base logged — and stops as soon as the read is exhausted.
    """

    def __init__(self, conn):
        self._conn = conn
        # A SET, not a flag: `_rescore_user` opens and closes cursors of its own, and a
        # single flag measured the last cursor instead of the open one — which is why the
        # first version of this test passed against the very defect it was written for.
        self.open_cursors: set[int] = set()

    async def execute(self, sql, params=()):
        head = sql.lstrip().split(None, 1)[0].upper()
        if head == "SELECT":
            cursor = _TrackingCursor(await self._conn.execute(sql, params), self)
            self.open_cursors.add(id(cursor))
            return cursor
        if self.open_cursors:
            raise sqlite3.OperationalError("database is locked")
        return await self._conn.execute(sql, params)

    async def commit(self):
        return await self._conn.commit()

    async def rollback(self):
        return await self._conn.rollback()


class _TrackingCursor:
    def __init__(self, raw, owner):
        self._raw = raw
        self._owner = owner

    def _done(self):
        self._owner.open_cursors.discard(id(self))

    async def fetchall(self):
        rows = await self._raw.fetchall()
        self._done()
        return rows

    async def fetchmany(self, n):
        rows = await self._raw.fetchmany(n)
        if not rows:
            self._done()
        return rows


class _BusyOnceThenFine:
    """The first write of each kind raises BUSY, as a competing writer would."""

    def __init__(self, conn, busy_writes=1):
        self._conn = conn
        self._left = busy_writes

    async def execute(self, sql, params=()):
        head = sql.lstrip().split(None, 1)[0].upper()
        if head in ("UPDATE", "INSERT") and self._left > 0:
            self._left -= 1
            raise sqlite3.OperationalError("database is locked")
        return await self._conn.execute(sql, params)

    async def commit(self):
        return await self._conn.commit()

    async def rollback(self):
        return await self._conn.rollback()


async def _seed_one_rescorable(cm):
    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("alice", "k", "redis cluster pipelining", 0.4, time.time(), time.time()),
    )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_a_write_is_not_issued_while_a_read_cursor_is_open(cm, monkeypatch):
    """The user list must be read out before any write, not iterated across them.

    This is the defect exactly: the loop held `cursor.fetchmany()` open for the whole
    pass, so the first UPDATE had to upgrade a read snapshot — the state SQLite refuses
    with BUSY no matter how long `busy_timeout` is.
    """
    await _seed_one_rescorable(cm)
    conn = await cm.get("memory.db")
    tracker = _WriteBlockedWhileACursorIsOpen(conn)

    async def get(_name):
        return tracker

    monkeypatch.setattr(cm, "get", get)
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    stats = await s.run_once()

    assert stats["errors"] == 0, f"a write must not be issued into an open read cursor: {stats}"
    assert stats["rescored"] >= 1, stats


@pytest.mark.asyncio
async def test_a_busy_write_is_retried_instead_of_losing_the_user(cm, monkeypatch):
    """A writer that holds the file for a moment must cost a retry, not the user.

    `busy_timeout` is already 15 s on this connection and did not help on the live base
    — the error came back in a tenth of a millisecond — because SQLite skips the handler
    when a read snapshot has to be upgraded. An explicit retry is the remedy that works
    in that window.
    """
    await _seed_one_rescorable(cm)
    conn = await cm.get("memory.db")
    flaky = _BusyOnceThenFine(conn)

    async def get(_name):
        return flaky

    monkeypatch.setattr(cm, "get", get)
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    stats = await s.run_once()

    assert stats["errors"] == 0, f"one BUSY must not fail the user: {stats}"
    assert stats["rescored"] >= 1, stats


@pytest.mark.asyncio
async def test_the_sweep_reports_what_it_did(cm, monkeypatch, caplog):
    """A pass must say what it did, so a partial one is visible from outside.

    It reported nothing before: a sweep that rescored twelve rows and one that failed on
    every user produced the same silence, and the failure's only trace was a traceback.
    """
    await _seed_one_rescorable(cm)
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    with caplog.at_level(logging.INFO):
        stats = await s.run_once()

    assert "importance sweep" in caplog.text, f"the pass must log its result: {caplog.text!r}"
    assert f"errors={stats['errors']}" in caplog.text, caplog.text
    assert f"rescored={stats['rescored']}" in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_an_interrupted_pass_keeps_what_it_already_committed(cm, monkeypatch):
    """Commits are per user, so an interruption keeps what already landed.

    With a single commit at the end of the pass, one collision discarded every row the
    pass had rescued — and the pass holds hundreds.
    """
    conn = await cm.get("memory.db")
    for uid, key in (("alice", "a"), ("bob", "b")):
        await conn.execute(
            """INSERT INTO core_memory
               (user_id, "key", value, importance, updated_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid, key, "redis cluster pipelining", 0.4, time.time(), time.time()),
        )
    await conn.commit()

    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))

    # The pass is interrupted after the first user's work is done. `CancelledError` is
    # not an `Exception`, so the per-user handler does not swallow it — the pass really
    # stops here, exactly as it does when the process is restarted mid-sweep.
    original = s._rescore_user
    seen = []

    async def flaky(uid, conn, stats):
        seen.append(uid)
        if len(seen) == 2:
            raise asyncio.CancelledError("the pass was interrupted")
        return await original(uid, conn, stats)

    monkeypatch.setattr(s, "_rescore_user", flaky)

    with pytest.raises(asyncio.CancelledError):
        await s.run_once()

    assert len(seen) == 2, f"the pass must have reached the second user: {seen}"

    # Read the FILE, not the connection that still holds the pass's uncommitted
    # transaction: a connection sees its own uncommitted writes, which is what made the
    # first version of this test happy against the defect. Closing the manager first is
    # what an interruption does to anything not yet committed.
    await cm.close_all()
    check = AsyncConnectionManager(base_dir=str(cm.base_dir))
    conn2 = await check.get("memory.db")
    row = await (await conn2.execute("SELECT count(*) c FROM importance_audit")).fetchone()
    await check.close_all()

    assert row is not None
    n = row["c"]
    assert n >= 1, "an interrupted pass must keep what it already committed"
