"""The WAL is never truncated, because the only TRUNCATE sits behind a VACUUM.

The defect.

`features/db_maintenance.py:59` is the only `PRAGMA wal_checkpoint(TRUNCATE)` in the
codebase, and it runs inside `_vacuum_if_fragmented` — which returns before reaching
it unless `freelist / page_count >= 0.25`. A base with no dead pages therefore never
truncates its WAL at all.

Measured on the live hermes base, 15.10.2026:

    memory.db       103.93 MB
    memory.db-wal    61.04 MB   (ceiling 64 MiB, from journal_size_limit)
    freelist_count          0   -> ratio 0.0000, so VACUUM always declines
    memory.db-wal   61.04 MB   -> unchanged across two restarts

The WAL is not growing out of control — `journal_size_limit` caps it at 64 MiB — and
that is exactly why it was never noticed: it sits at its cap looking stable. What it
costs is 61 MB of disk held for nothing, plus a recovery step that has to replay it
after a crash. And a clean connection close deletes the file, so the cost only shows
while the server is running — which, for this base, is measured in weeks.

A PASSIVE checkpoint is not the fix, and the probe that established that is worth
recording: it moved all 806 journal pages into the database and left the WAL file the
same size on disk. Moving pages is not shrinking the file. Only TRUNCATE gives the
space back.
"""

from __future__ import annotations

import sqlite3

import pytest

from features.db_maintenance import checkpoint_wal, run_db_maintenance
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def cm(tmp_path, monkeypatch):
    """Isolated base dir, connection_manager pointed at it (matches test_db_maintenance)."""
    manager = AsyncConnectionManager(base_dir=str(tmp_path))
    from shared import connection as conn_mod

    orig_dir = conn_mod.connection_manager.base_dir
    orig_conns = dict(conn_mod.connection_manager._conns)
    conn_mod.connection_manager.base_dir = tmp_path
    conn_mod.connection_manager._conns.clear()
    yield manager
    conn_mod.connection_manager.base_dir = orig_dir
    conn_mod.connection_manager._conns.clear()
    conn_mod.connection_manager._conns.update(orig_conns)


async def _big_wal_no_fragmentation(cm, name: str = "memory.db") -> tuple:
    """An OPEN connection with a fat WAL and zero dead pages — the live condition.

    The connection stays open on purpose: closing the last one deletes the WAL file
    outright, which would hide both the defect and the fix.
    """
    conn = await cm.get(name)
    await conn.execute("CREATE TABLE t (v TEXT)")
    await conn.executemany("INSERT INTO t VALUES (?)", [("y" * 3000,)] * 4000)
    await conn.commit()
    return conn


def _wal_bytes(base, name: str = "memory.db") -> int:
    wal = base / f"{name}-wal"
    return wal.stat().st_size if wal.exists() else 0


@pytest.mark.asyncio
async def test_maintenance_truncates_the_wal_without_fragmentation(cm, tmp_path):
    """No dead pages -> VACUUM declines -> the WAL must still be given back."""
    await _big_wal_no_fragmentation(cm)

    before = _wal_bytes(tmp_path)
    assert before > 5 * 1024 * 1024, f"fixture must leave a big WAL, got {before} bytes"

    # Confirm the state that makes the defect silent: nothing to vacuum.
    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.close()
    assert freelist == 0 and pages > 0, "fixture must be unfragmented"

    await run_db_maintenance(cm=cm, vacuum_min_mb=1)

    after = _wal_bytes(tmp_path)
    assert after < 1024 * 1024, (
        f"WAL was {before / 1048576:.1f} MB before and {after / 1048576:.1f} MB after maintenance; "
        "with no dead pages the checkpoint is unreachable, so the file never shrinks"
    )


@pytest.mark.asyncio
async def test_checkpoint_reports_what_it_reclaimed(cm, tmp_path):
    """The guard must be visible: a caller learns how much came back."""
    await _big_wal_no_fragmentation(cm)

    reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")

    assert reclaimed > 5.0, f"expected the WAL's megabytes back, got {reclaimed:.2f}"
    assert _wal_bytes(tmp_path) < 1024 * 1024


@pytest.mark.asyncio
async def test_checkpoint_is_best_effort(cm, tmp_path, monkeypatch):
    """A checkpoint that cannot complete must report zero, not raise.

    TRUNCATE needs every reader out of the way and reports `busy=1` rather than
    raising when one is not. This runs on a maintenance path where raising would
    cost the whole pass — including the size report that follows it.
    """
    await _big_wal_no_fragmentation(cm)

    monkeypatch.setattr("features.db_maintenance._run_checkpoint", lambda _p: (1, 806, 806))

    reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")

    assert reclaimed == 0.0, "a busy checkpoint reclaimed nothing and must say so"


@pytest.mark.asyncio
async def test_checkpoint_swallows_a_raising_call(cm, tmp_path, monkeypatch):
    """If the PRAGMA itself blows up, the pass must survive it.

    Kept separate from the busy case: `busy` is SQLite declining, an exception is
    something else going wrong, and the two take different paths through the code.
    """
    await _big_wal_no_fragmentation(cm)
    before = _wal_bytes(tmp_path)

    def _exploding(_path):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("features.db_maintenance._run_checkpoint", _exploding)

    reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")

    assert reclaimed == 0.0, "a failed checkpoint must report zero, not raise"
    assert _wal_bytes(tmp_path) == before, "nothing should have been written"


@pytest.mark.asyncio
async def test_checkpoint_retries_past_a_transient_lock(cm, tmp_path):
    """A lock held for a moment must not cost the whole truncation.

    The live pass ran twice with no effect and no log line. Reproduced on a copy:
    with another connection holding a read snapshot the PRAGMA does not report
    `busy=1`, it RAISES `sqlite3.OperationalError: database table is locked`. One
    attempt therefore loses to any brief reader — and readers here are brief.
    """
    import asyncio

    await _big_wal_no_fragmentation(cm)
    reader = await cm.get("memory.db")
    await reader.execute("BEGIN")
    await (await reader.execute("SELECT COUNT(*) FROM t")).fetchall()  # open snapshot

    before = _wal_bytes(tmp_path)
    assert before > 5 * 1024 * 1024

    async def _release_soon() -> None:
        await asyncio.sleep(0.3)
        await reader.execute("ROLLBACK")

    task = asyncio.create_task(_release_soon())
    try:
        reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")
    finally:
        await task

    assert reclaimed > 5.0, (
        f"the reader released after 0.3 s and the WAL was {before / 1048576:.1f} MB, "
        f"but only {reclaimed:.2f} MB came back — one attempt is not enough"
    )
    assert _wal_bytes(tmp_path) < 1024 * 1024


@pytest.mark.asyncio
async def test_checkpoint_says_so_when_it_cannot_truncate(cm, tmp_path, caplog, monkeypatch):
    """Failing to truncate must be visible.

    The previous version logged the failure at DEBUG while the live config is INFO,
    so a base whose WAL never shrank looked exactly like one with nothing to shrink.
    A silent no-op is the defect this whole file exists to catch.
    """
    import logging

    await _big_wal_no_fragmentation(cm)
    size = _wal_bytes(tmp_path)

    def _locked(_path):
        raise sqlite3.OperationalError("database table is locked")

    monkeypatch.setattr("features.db_maintenance._run_checkpoint", _locked)

    with caplog.at_level(logging.WARNING):
        reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")

    assert reclaimed == 0.0
    assert _wal_bytes(tmp_path) == size, "nothing should have been written"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("checkpoint" in r.getMessage().lower() for r in warnings), (
        f"a failed truncation of a {size / 1048576:.1f} MB WAL produced no warning: {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_checkpoint_keeps_quiet_about_a_small_wal(cm, tmp_path, caplog, monkeypatch):
    """A small journal is normal and must not warn.

    An alarm that fires every hour regardless is the defect just removed from
    `db_warn_mb`; this guard must not reintroduce it at a smaller scale.
    """
    import logging

    conn = await cm.get("memory.db")
    await conn.execute("CREATE TABLE t (v TEXT)")
    await conn.execute("INSERT INTO t VALUES ('x')")
    await conn.commit()
    assert 0 < _wal_bytes(tmp_path) < 1024 * 1024, "fixture must leave a SMALL, non-empty WAL"

    def _locked(_path):
        raise sqlite3.OperationalError("database table is locked")

    monkeypatch.setattr("features.db_maintenance._run_checkpoint", _locked)

    with caplog.at_level(logging.WARNING):
        await checkpoint_wal(cm, tmp_path / "memory.db")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"a small WAL should pass without a word, got: {[r.getMessage() for r in warnings]}"


@pytest.mark.asyncio
async def test_maintenance_leaves_foreign_databases_alone(cm, tmp_path):
    """Only this process's own DBs are checkpointed.

    The size loop walks every `*.db` in the base dir and some belong to other tools.
    Truncating a WAL is a write, so it stays scoped to the same two files the
    existing VACUUM already refuses to touch elsewhere. The foreign connection is
    held OPEN so its WAL survives the check — closing it would delete the file and
    the test would pass for the wrong reason.
    """
    foreign = tmp_path / "someone_elses.db"
    holder = sqlite3.connect(str(foreign))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE t (v TEXT)")
    holder.executemany("INSERT INTO t VALUES (?)", [("z" * 3000,)] * 4000)
    holder.commit()
    before = _wal_bytes(tmp_path, "someone_elses.db")
    assert before > 5 * 1024 * 1024, f"fixture must leave a big foreign WAL, got {before}"

    try:
        await run_db_maintenance(cm=cm, vacuum_min_mb=1)
        after = _wal_bytes(tmp_path, "someone_elses.db")
        assert after == before, f"maintenance truncated a foreign database's WAL: {before} -> {after}. It must only touch the DBs this process owns."
    finally:
        holder.close()
