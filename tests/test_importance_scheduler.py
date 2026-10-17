"""Tests for importance scheduler."""

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
async def test_the_pass_never_touches_the_shared_connection(cm, monkeypatch):
    """The sweep must open its own connection, not borrow the handlers'.

    Measured consequence of borrowing (a borrowed row was published by the pass's commit
    and another vanished on its rollback), and the reason `_rescore_pass` moved to
    `sqlite3.connect` + `asyncio.to_thread`. Asserting on `get` rather than on a proxy is
    the point: a pass that never asks cannot publish or discard anyone else's work, and a
    proxy test would pass for a pass that quietly opened the shared file some other way.
    """
    await _seed_one_rescorable(cm)

    calls: list[str] = []
    real_get = cm.get

    async def get(name):
        calls.append(name)
        return await real_get(name)

    monkeypatch.setattr(cm, "get", get)
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    stats = await s.run_once()

    assert calls == [], f"the sweep borrowed the shared connection: {calls}"
    assert stats["errors"] == 0, stats
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


# --- the pass must not reach outside its own transaction ---------------------------
#
# #129 added a commit per user and a rollback in the error path, both on the connection
# every tool handler shares. Measured on a throwaway base: a handler's uncommitted row
# was published by the per-user commit, and another one vanished on the rollback. The
# pass runs on a borrowed connection, so it owns no transaction boundary — these two
# tests pin that, because losing a handler's row is worse than a failed sweep.


@pytest.mark.asyncio
async def test_the_pass_does_not_roll_back_foreign_work(cm, monkeypatch):
    """A failure inside the pass must not discard a handler's uncommitted row.

    The pass borrows the shared connection; a rollback there reaches every task that
    has written on it and not yet committed.
    """
    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES ('alice', 'k', 'redis cluster pipelining', 0.4, ?, ?)""",
        (time.time(), time.time()),
    )
    await conn.commit()

    # A handler's write that has NOT been committed when the sweep fails.
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES ('handler', 'pending', 'work in progress', 0.5, ?, ?)""",
        (time.time(), time.time()),
    )

    def boom(uid, c, stats):
        raise sqlite3.OperationalError("database is locked")

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    monkeypatch.setattr(s, "_rescore_user", boom)
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)

    stats = await s.run_once()

    assert stats["errors"] >= 1, stats
    row = await (await conn.execute("SELECT count(*) c FROM core_memory WHERE \"key\"='pending'")).fetchone()
    assert row is not None
    assert row["c"] == 1, "the sweep's error path must not discard a handler's uncommitted row"


@pytest.mark.asyncio
async def test_the_pass_does_not_publish_foreign_work_early(cm, monkeypatch):
    """Rows must become visible to other readers only at the pass's single commit.

    With a commit per user, everything a handler had written and not committed went out
    with the first user's commit — work published before its owner decided to.
    """

    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES ('alice', 'k', 'redis cluster pipelining', 0.4, ?, ?)""",
        (time.time(), time.time()),
    )
    await conn.commit()

    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES ('handler', 'pending', 'work in progress', 0.5, ?, ?)""",
        (time.time(), time.time()),
    )

    # Fail the FIRST user, so the pass never reaches its final commit.
    def boom(uid, c, stats):
        raise sqlite3.OperationalError("database is locked")

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    monkeypatch.setattr(s, "_rescore_user", boom)
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)

    await s.run_once()

    # Ask from a different connection: the handler's row must still be invisible.
    #
    # A plain read-only handle, not another AsyncConnectionManager: creating a managed
    # connection runs `PRAGMA journal_mode=WAL`, which needs a write lock — and the
    # handler's open transaction holds one. That is a separate defect (see the note at the
    # end of this file), and it must not be what this test measures.
    import asyncio as _asyncio

    def _read_pending() -> int:
        ro = sqlite3.connect(f"file:{cm.base_dir / 'memory.db'}?mode=ro", uri=True)
        try:
            return int(ro.execute("SELECT count(*) FROM core_memory WHERE \"key\"='pending'").fetchone()[0])
        finally:
            ro.close()

    n = await _asyncio.to_thread(_read_pending)

    assert n == 0, "a failing pass must not publish a handler's uncommitted row"


@pytest.mark.asyncio
async def test_the_sweep_closes_its_own_connection(cm, monkeypatch):
    """A pass must not leave a descriptor behind, and must not leave a lock behind.

    The sweep now opens a short-lived `sqlite3` connection. If it leaked that handle the
    cause of this whole change would be reintroduced in a new place — one descriptor per
    pass, every thirty minutes, for the life of the daemon.
    """
    import gc
    import os

    await _seed_one_rescorable(cm)
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)

    def fds() -> int:
        target = str(cm.base_dir / "memory.db")
        n = 0
        for fd in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{fd}") == target:
                    n += 1
            except OSError:
                continue
        return n

    gc.disable()
    try:
        s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
        await s.run_once()  # warm: migrations, first-run branches
        before = fds()
        for _ in range(5):
            await s.run_once()
        assert fds() == before, "the sweep left its connection open"
    finally:
        gc.enable()
