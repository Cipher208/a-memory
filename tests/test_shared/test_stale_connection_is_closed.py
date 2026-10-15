"""A connection dropped from `_conns` must be CLOSED, not merely forgotten.

The defect.

`AsyncConnectionManager.get()` treats a connection as stale when `SELECT 1` fails
on it, and then does:

    self._conns.pop(db_name, None)

The connection object is released from the dict and nothing else happens to it. But
`_get_aiosqlite_conn` appends every connection to `_tracked_aiosqlite_conns`, a
**strong** list — deliberately, as its own comment records ("a WeakSet would lose
it", because the aiosqlite worker thread keeps only the tx queue alive). So a
dropped connection is not garbage collected. If it was holding a transaction when
it went stale, it goes on holding that transaction's locks for the life of the
process, with no path back to it: nothing in the manager references it any more,
so nothing will ever close it.

Measured context, live hermes base 15.10.2026: decay ran clean from 09:00 to 16:02,
then failed on `sqlite3.OperationalError: database is locked` **ten times in a row**
over two hours, while the WAL grew from 2 MB to 14.5 MB. Probing the write lock from
OUTSIDE returned free eight times out of eight — the lock was held inside the
process, which is exactly what a leaked connection looks like from the outside.

A restart cleared it, which is consistent with the connection being unreachable:
killing the process is the only thing that releases it.

Two properties are pinned here:

  1. the stale connection is closed;
  2. a replacement someone else installed is NOT closed.
"""

from __future__ import annotations

import sqlite3

import pytest

from shared.connection import AsyncConnectionManager


class _StaleProxy:
    """Stands in for a connection that has gone bad while holding a real one.

    Wraps a REAL aiosqlite connection so the lock it holds is real, and raises only
    on the `SELECT 1` that `get()` uses as its liveness probe. Anything else is
    delegated: if `close()` is never called, the underlying connection keeps its
    transaction and its lock, which is the whole point of the test.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.closed = False

    async def execute(self, sql, *args, **kwargs):
        if sql.strip().upper() == "SELECT 1":
            raise sqlite3.OperationalError("connection went stale (simulated)")
        return await self._real.execute(sql, *args, **kwargs)

    async def close(self) -> None:
        self.closed = True
        await self._real.close()

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction


@pytest.fixture
async def cm(tmp_path):
    manager = AsyncConnectionManager(base_dir=str(tmp_path))
    yield manager
    await manager.close_all()


async def test_stale_connection_is_closed_not_just_dropped(cm, tmp_path):
    """The fix's contract, on the real code path.

    A connection that failed its liveness probe is still holding whatever it was
    holding — if it is dropped without `close()`, nothing can ever release that.
    """
    real = await cm.get("memory.db")
    stale = _StaleProxy(real)
    cm._conns["memory.db"] = stale

    fresh = await cm.get("memory.db")

    assert stale.closed, (
        "the stale connection was dropped from _conns without being closed — it is "
        "still tracked by _tracked_aiosqlite_conns (strong refs), so nothing will "
        "ever release the locks it holds"
    )
    assert cm._conns["memory.db"] is fresh
    assert fresh is not stale


async def test_a_leaked_stale_connection_keeps_its_lock(cm, tmp_path):
    """Why that matters — with a real lock, not an assertion about bookkeeping.

    The stale connection holds an uncommitted write transaction. After the manager
    hands out a fresh connection, a write must go through. On the leaking code it
    does not: the dropped connection still owns the lock.
    """
    real = await cm.get("memory.db")
    await real.execute("CREATE TABLE IF NOT EXISTS probe (v INTEGER)")
    await real.commit()
    # Hold a write transaction on the connection that is about to go stale.
    await real.execute("BEGIN IMMEDIATE")
    await real.execute("INSERT INTO probe VALUES (1)")

    cm._conns["memory.db"] = _StaleProxy(real)
    fresh = await cm.get("memory.db")

    # Keep the failure fast: the manager sets a 15 s busy_timeout, and a leaked lock
    # would otherwise sit there for the full wait before reporting.
    await fresh.execute("PRAGMA busy_timeout=300")
    try:
        await fresh.execute("BEGIN IMMEDIATE")
        await fresh.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:  # pragma: no cover - the failing branch
        pytest.fail(
            f"a fresh connection could not write while the dropped one held a transaction: {exc}. That is the leaked connection keeping its lock."
        )
    finally:
        with __import__("contextlib").suppress(Exception):
            await real.execute("ROLLBACK")


class _RecordingDict(dict):
    """A dict that records evictions.

    `dict.pop` cannot be monkeypatched on an instance — assigning to it raises
    `AttributeError: 'dict' object attribute 'pop' is read-only`. The first version of
    the replacement test did that and "failed", which made the mutation run look
    green for the wrong reason: the test was erroring, not asserting. Subclassing is
    the way in.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.popped: list[tuple] = []

    def pop(self, key, *args):
        value = super().pop(key, *args)
        self.popped.append((key, value))
        return value


async def test_a_replacement_installed_by_someone_else_is_left_alone(cm, tmp_path):
    """The reason the guard exists, asserted on the mechanism that matters.

    Two earlier versions of this test were wrong, and the mutation run is what said
    so:

    * `not replacement.closed` alone passes with the guard removed, because the code
      always closes `conn` — the object it judged stale — never the replacement;
    * asserting `_conns["memory.db"] is replacement` at the end fails even on correct
      code, because `get()` goes on to install a freshly opened connection over it.

    What the guard actually prevents is the EVICTION: with it gone, `pop` removes
    whatever is in the dict — the replacement — and that connection is then
    unreachable while still tracked by `_tracked_aiosqlite_conns`. Same leak class
    as the bug being fixed, one level out.
    """
    real = await cm.get("memory.db")
    stale = _StaleProxy(real)
    replacement = _StaleProxy(real)

    recording = _RecordingDict(cm._conns)
    cm._conns = recording
    recording["memory.db"] = stale

    original_execute = stale.execute

    async def _replacing_execute(sql, *args, **kwargs):
        recording["memory.db"] = replacement  # the concurrent getter wins
        return await original_execute(sql, *args, **kwargs)

    stale.execute = _replacing_execute

    await cm.get("memory.db")

    evicted = [value for key, value in recording.popped if key == "memory.db"]
    assert replacement not in evicted, (
        "this call evicted a connection another getter had just installed — it is now "
        "unreachable while still strongly tracked, so nothing will ever close it"
    )
    assert not replacement.closed, "the replacement must not be closed either"


async def test_stale_close_does_not_raise_if_close_fails(cm, tmp_path):
    """Closing is best-effort: a manager that raises here would break every caller."""
    real = await cm.get("memory.db")

    class _BadClose(_StaleProxy):
        async def close(self) -> None:
            raise RuntimeError("close blew up")

    cm._conns["memory.db"] = _BadClose(real)

    conn = await cm.get("memory.db")  # must not raise

    assert conn is not None
    with __import__("contextlib").suppress(Exception):
        await real.close()
