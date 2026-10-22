"""Tests for leaked aiosqlite worker-thread cleanup (os._exit removal)."""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from shared.connection import AsyncConnectionManager, leaked_connection_workers

#: How long a condition wait may take before it is called a failure.
_WAIT_TIMEOUT = 5.0
#: How often the condition is polled while waiting.
_WAIT_INTERVAL = 0.01


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = _WAIT_TIMEOUT, interval: float = _WAIT_INTERVAL) -> bool:
    """Wait until `predicate` holds, or the deadline passes.

    The aiosqlite worker is a separate thread. `stop()` only queues the stop
    sentinel; the sqlite handle is closed later, when that thread gets round
    to the queue. A fixed sleep therefore races the worker: it was measured at
    ~1.5 ms in isolation, but CI runs 2400+ tests with several interpreters in
    parallel, and 200 ms was not always enough — the test then failed on
    correct code. Polling the condition against a deadline replaces the guess
    with a bound.
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


async def _open_two_conns(base: Path) -> AsyncConnectionManager:
    cm = AsyncConnectionManager(base_dir=base)
    c1 = await cm.get("a.db")
    await c1.execute("CREATE TABLE IF NOT EXISTS t(x)")
    c2 = await cm.get("b.db")
    await c2.execute("CREATE TABLE IF NOT EXISTS t(x)")
    # fixture-style leak: drop handles without close()
    cm._conns.clear()
    return cm


@pytest.mark.asyncio
async def test_clear_without_close_leaks_workers(tmp_path: Path) -> None:
    """Reproduce the leak: clear-only teardown leaves aiosqlite workers running.

    Asserts against the module's own leaked-connection registry, not against
    threading.active_count(). The global count is shared state and this test read
    it twice; the gate caught it as `assert 9 > 11` — two threads belonging to
    earlier tests exited inside the window and swallowed the two leaked here, so
    the check failed under the gate's load while passing in isolation. That is
    the kind of flake that teaches people to bypass the gate.

    The registry names exactly the connections under test, and each thread is
    checked to be genuinely alive, which is this test's actual claim: the leak is
    real threads, not bookkeeping.
    """
    await _open_two_conns(tmp_path)
    leaked = leaked_connection_workers()
    assert len(leaked) >= 2, "clear() without close() must leave the workers behind"
    assert all(c._thread.is_alive() for c in leaked), "a leaked connection has no live worker thread"


@pytest.mark.asyncio
async def test_leaked_connection_workers_reports_and_stops(tmp_path: Path) -> None:
    """leaked_connection_workers() finds them; stop stops them for real."""
    await _open_two_conns(tmp_path)
    await asyncio.sleep(0.2)
    leaked = leaked_connection_workers()
    assert len(leaked) >= 2  # one worker per leaked connection

    leaked_connection_workers(stop=True)
    await asyncio.sleep(0.2)
    assert not leaked_connection_workers(), "workers must be gone after stop"


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: Path) -> None:
    await _open_two_conns(tmp_path)
    await asyncio.sleep(0.2)
    leaked_connection_workers(stop=True)
    # second call on empty registry must not raise
    leaked_connection_workers(stop=True)
    assert not leaked_connection_workers()


@pytest.mark.asyncio
async def test_live_connections_not_stopped(tmp_path: Path) -> None:
    """Connections still tracked in _conns must NOT be force-stopped by cleanup."""
    cm = AsyncConnectionManager(base_dir=tmp_path)
    conn = await cm.get("live.db")
    await conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
    leaked_connection_workers(stop=True)
    await asyncio.sleep(0.2)
    # live conn still usable
    await conn.execute("SELECT 1")
    await cm.close_all()


@pytest.mark.asyncio
async def test_force_stops_even_owned_connections(tmp_path: Path) -> None:
    """force=True stops workers whose manager still holds them (teardown path)."""
    cm = AsyncConnectionManager(base_dir=tmp_path)
    conn = await cm.get("owned.db")
    await conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
    leaked = leaked_connection_workers(stop=True)  # gentle: owned -> untouched
    assert all(c is not conn for c in leaked)
    forced = leaked_connection_workers(stop=True, force=True)
    assert any(c is conn for c in forced)
    # The worker closes the handle when it processes the sentinel, on its own
    # thread, so wait for that to happen instead of guessing a delay.
    # Do NOT await conn.execute here — its worker thread is gone, the call
    # would park forever on the drained queue.
    assert await _wait_until(lambda: conn._connection is None), "the worker must close the sqlite handle after the stop sentinel"
