"""The checkpoint must use its own connection, not the shared one.

Proven by probe, then pinned here.

The shared connection is used by every MCP tool handler, and every `await` is a yield
point. A handler doing

    cur = await conn.execute("SELECT ... FROM some_table")
    rows = await cur.fetchall()      # <-- the maintenance pass can run in this gap

has a statement actively stepping. A `wal_checkpoint(TRUNCATE)` that lands in that gap,
ON THE SAME CONNECTION, raises:

    sqlite3.OperationalError: database table is locked

That is not `database is locked` (another writer) — it is a same-connection error, and
it is why the live checkpoint failed 7 times out of 29 while reporting nothing usable.
Measured, same base, statement in flight:

    same connection      -> OperationalError: database table is locked
    separate connection  -> ok, busy=1 log=59 done=59

On its OWN connection the same call answers `busy=1`: SQLite's normal "a reader holds
the WAL, ask again later". That is exactly what the retry loop was written to handle —
and it could never handle anything while every attempt raised instead.

So this is not a new feature; it is making the retry loop reachable.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from features.db_maintenance import checkpoint_wal
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def cm(tmp_path):
    manager = AsyncConnectionManager(base_dir=str(tmp_path))
    yield manager
    await manager.close_all()


def _wal_mb(base) -> float:
    p = base / "memory.db-wal"
    return p.stat().st_size / 1048576 if p.exists() else 0.0


def _db_fds(base) -> list[str]:
    """Descriptors this process holds on db files under `base`.

    Local on purpose: the health reader in shared/ is another change's business, and a
    test that imports it cannot run without that change.
    """
    out = []
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{entry}")
        except OSError:
            continue
        if ".db" in target and str(base) in target:
            out.append(target.replace(str(base) + "/", ""))
    return sorted(out)


async def _fat_wal(cm, rows: int = 15000):
    conn = await cm.get("memory.db")
    await conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    await conn.executemany("INSERT INTO t VALUES (?)", [("x" * 500,)] * rows)
    await conn.commit()
    return conn


async def test_failure_is_reported_as_busy_not_as_our_own_statement(cm, tmp_path, caplog):
    """The property that actually distinguishes the fix — and it is about reporting.

    The retries mask the success difference: with a reader that drains inside the 2 s
    window, BOTH the shared and the separate connection eventually truncate, so a test
    asserting "it succeeded" cannot tell them apart. That is exactly why the first
    version of this file let the mutation through.

    What differs is the failure when the reader does NOT drain in time:

      shared connection   -> OperationalError: database table is locked
                             (a same-connection error: our own statement is in the way)
      its own connection  -> busy=1
                             (SQLite's normal "a reader holds the WAL, ask again")

    Those two mean different things and lead to different places. The first sent me
    hunting our own statements for hours; the second says plainly that the base is busy.
    """
    import logging

    conn = await _fat_wal(cm)
    assert _wal_mb(tmp_path) >= 5.0, (
        f"the warning threshold is 5 MB; the fixture left {_wal_mb(tmp_path):.2f} MB and a smaller one would pass this test by saying nothing"
    )
    holder = await conn.execute("SELECT * FROM t")  # held for the whole retry window

    try:
        with caplog.at_level(logging.WARNING):
            reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")
    finally:
        await holder.fetchall()

    assert reclaimed == 0.0, "a held reader must leave the WAL untruncated"
    warning = next(
        (r.getMessage() for r in caplog.records if "WAL checkpoint" in r.getMessage()),
        None,
    )
    assert warning is not None, [r.getMessage() for r in caplog.records]
    assert "busy=" in warning, f"the failure should read as contention, not as our own statement: {warning}"
    assert "table is locked" not in warning, (
        f"the checkpoint is still on the shared connection — a statement of ours is what it tripped over: {warning}"
    )


async def test_each_checkpoint_closes_its_connection(cm, tmp_path):
    """Each call opens a connection; none may be left for the garbage collector.

    Measured with `gc.disable()` on purpose, and that detail is the whole test.

    An earlier version simply compared counts after 5 and after 20 calls and stayed
    green even with the `finally: other.close()` removed — because CPython's automatic
    cycle collection had run in between. Measured directly: a sqlite3 connection left
    unclosed holds its descriptor until a GC pass (`+1` immediately after the call,
    `0` after `gc.collect()`), so refcounting alone does not free it.

    With collection off, a missing close is unambiguous: every call leaks one.
    """
    import gc

    await _fat_wal(cm)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = _db_fds(tmp_path)
        for _ in range(5):
            await checkpoint_wal(cm, tmp_path / "memory.db")
        after = _db_fds(tmp_path)
    finally:
        if gc_was_enabled:
            gc.enable()

    assert len(after) <= len(before) + 1, (
        f"five checkpoints added {len(after) - len(before)} descriptors with the collector off: "
        f"{before} -> {after}. Each call must close its own connection."
    )


async def test_busy_flag_is_authoritative_even_if_the_file_shrank(cm, tmp_path, monkeypatch, caplog):
    """SQLite saying `busy` must be obeyed, not overridden by an encouraging file size.

    The guard being pinned is `if not busy and size_now < size_before`. Removing the
    `not busy` half survived every other test in this file, because SQLite does not
    normally return `busy=1` together with a shrink — so no reachable state
    distinguishes the two versions, and the guard reads as dead code.

    It is not dead code, it is a refusal to guess. `busy=1` means SQLite declined to
    truncate; if the file happens to be smaller for any other reason (another process
    truncated it, a checkpoint completed elsewhere), the honest report is still "we did
    not do this". Claiming the megabytes would put a number in the log that this call
    did not earn.

    Simulated by making the checkpoint truncate for real while reporting `busy=1` — the
    one state the guard exists for.
    """
    import logging

    import os

    import features.db_maintenance as dbm

    await _fat_wal(cm)  # connection stays open: the WAL survives
    wal = tmp_path / "memory.db-wal"
    assert _wal_mb(tmp_path) >= 5.0, "the warning path needs a WAL over the 5 MB threshold"

    def _truncate_but_report_busy(path):
        # Shrink the journal directly — no SQLite involved, so the file size genuinely
        # drops while the reported triple still says BUSY. That is the state the guard
        # exists for: the megabytes went away, but not because of this call.
        os.truncate(str(path) + "-wal", 0)
        return (1, 100, 0)

    monkeypatch.setattr(dbm, "_run_checkpoint", _truncate_but_report_busy)

    with caplog.at_level(logging.WARNING):
        reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")

    assert wal.stat().st_size == 0, "fixture: the file must really have shrunk"
    assert reclaimed == 0.0, (
        f"claimed {reclaimed:.2f} MB that SQLite reported as busy — the size of the file is not proof that THIS call reclaimed it"
    )
    assert any("busy=" in r.getMessage() for r in caplog.records), (
        f"the busy flag must be reported, not swallowed by an encouraging file size: {[r.getMessage() for r in caplog.records]}"
    )


async def test_checkpoint_does_not_raise_while_a_statement_is_in_flight(cm, tmp_path):
    """The live failure, reproduced: a handler mid-statement, maintenance arriving."""
    conn = await _fat_wal(cm)
    assert _wal_mb(tmp_path) > 1, "fixture must leave a real WAL"

    # A statement in flight on the shared connection — an ordinary handler between its
    # execute() and its fetch().
    holder = await conn.execute("SELECT * FROM t")

    async def release_soon():
        await asyncio.sleep(0.3)
        await holder.fetchall()

    task = asyncio.create_task(release_soon())
    try:
        # Must not raise. On the shared connection this raised
        # `database table is locked`; on its own it answers busy=1 and retries.
        reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")
    finally:
        await task

    assert reclaimed >= 0, "the return value must be a number, not an exception"
    # The first call may already have won once the reader drained (the retry loop is
    # 4 attempts over 2 s); if it did not, a following call must. What must NOT happen
    # is a WAL still sitting at its old size with both calls reporting nothing — that
    # is the shared-connection failure mode.
    total = reclaimed + await checkpoint_wal(cm, tmp_path / "memory.db")
    assert total > 1.0, (
        f"the WAL was {_wal_mb(tmp_path):.2f} MB and both calls together reclaimed {total:.2f} of it — the checkpoint never reached the WAL"
    )


async def test_checkpoint_still_reclaims_when_nothing_is_in_flight(cm, tmp_path):
    """The other half: a quiet base must truncate as before."""
    await _fat_wal(cm)

    reclaimed = await checkpoint_wal(cm, tmp_path / "memory.db")

    assert reclaimed > 1.0, f"expected the WAL back, got {reclaimed:.2f}"
    assert _wal_mb(tmp_path) < 0.5
