"""Tests for leaked aiosqlite worker-thread cleanup (os._exit removal)."""

import asyncio
import threading
from pathlib import Path

import pytest

from shared.connection import AsyncConnectionManager, leaked_connection_workers


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
    """Reproduce the leak: clear-only teardown leaves aiosqlite workers running."""
    before = threading.active_count()
    await _open_two_conns(tmp_path)
    await asyncio.sleep(0.2)
    assert threading.active_count() > before, "expected leaked workers after clear() without close"


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
    await asyncio.sleep(0.2)
    # worker processed the sentinel: sqlite handle closed synchronously.
    # Do NOT await conn.execute here — its worker thread is gone, the call
    # would park forever on the drained queue.
    assert conn._connection is None
