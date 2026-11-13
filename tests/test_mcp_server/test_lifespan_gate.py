"""Startup gate: migrations serialize on the maintenance turnstile (spec S3)."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import threading
import time

import pytest

from mcp_server.lifespan import run_startup_gated


def _hold(tmp_path, seconds: float) -> threading.Thread:
    """Stand in for another process mid-migration; release after `seconds`."""
    fh = open(tmp_path / ".maintenance.lock", "w")  # noqa: SIM115
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _release() -> None:
        time.sleep(seconds)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    thread = threading.Thread(target=_release)
    thread.start()
    return thread


async def test_gate_waits_for_another_writer(tmp_path) -> None:
    thread = _hold(tmp_path, 0.3)
    ran: list[str] = []

    async def migrate() -> str:
        ran.append("migrate")
        return "ok"

    def sync() -> None:
        ran.append("sync")

    start = time.monotonic()
    await run_startup_gated(migrate, sync, base_dir=tmp_path, gate_timeout=5.0)
    waited = time.monotonic() - start
    thread.join(timeout=5)
    assert ran == ["migrate", "sync"], "both startup writes must run, in order"
    assert waited >= 0.25, f"gate did not wait for the holder ({waited:.2f}s)"


async def test_gate_timeout_falls_back_to_ungated(caplog, tmp_path) -> None:
    # A migration that never lets go must not keep the server from coming up:
    # fall back to today's behavior, loudly (spec S3).
    fh = open(tmp_path / ".maintenance.lock", "w")  # noqa: SIM115, ASYNC230
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        ran: list[str] = []

        async def migrate() -> str:
            ran.append("migrate")
            return "ok"

        def sync() -> None:
            ran.append("sync")

        with caplog.at_level(logging.WARNING, logger="mcp_server.lifespan"):
            await run_startup_gated(migrate, sync, base_dir=tmp_path, gate_timeout=0.3)
        assert ran == ["migrate", "sync"], "the fallback must still run migrations"
        assert "ungated" in caplog.text, "the fallback must be logged as a warning"
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


async def test_gate_releases_the_turnstile_for_the_next_process(tmp_path) -> None:
    from shared.maintenance_lock import acquire

    await run_startup_gated(
        (lambda: asyncio.sleep(0, "ok")),
        lambda: None,
        base_dir=tmp_path,
        gate_timeout=5.0,
    )
    fd = acquire(tmp_path)
    assert fd is not None, "the gate must hand the turnstile back"
    from shared.maintenance_lock import release

    release(fd)


async def test_gate_releases_even_when_migrate_raises(tmp_path) -> None:
    # The finally-release is load-bearing: a leaked turnstile would silently
    # stop housekeeping for every other process on this base.
    async def migrate() -> str:
        raise RuntimeError("migration blew up")

    with pytest.raises(RuntimeError, match="blew up"):
        await run_startup_gated(migrate, lambda: None, base_dir=tmp_path, gate_timeout=5.0)
    from shared.maintenance_lock import acquire

    fd = acquire(tmp_path)
    assert fd is not None, "turnstile must come back even when migrations raise"
    from shared.maintenance_lock import release

    release(fd)


def test_startup_gate_is_bounded_below_the_client_handshake_budget() -> None:
    """The gate must never outlast the client's handshake wait.

    2026-11-13: a stdio server blocked in `maintenance_lock.acquire()` for the
    full 120 s default while the opencode v2 client gives up on `initialize`
    after ~6 s. The server was alive and polling the lock, but the client had
    already declared `mcp transport error ... process exited` — so the memory
    server was invisible for every session that started during the wedge.

    The gate's own docstring already says the fallback is "better a noisy
    success than a server that never comes up". A 120 s wait contradicts that:
    the server is up, yet unreachable. Bound the wait to less than the client
    budget so the ungated fallback actually runs while the client is listening.
    """
    from mcp_server import lifespan

    assert lifespan.MIGRATION_GATE_TIMEOUT_S < lifespan.CLIENT_HANDSHAKE_BUDGET_S, (
        f"startup gate ({lifespan.MIGRATION_GATE_TIMEOUT_S}s) must be shorter than "
        f"the client handshake budget ({lifespan.CLIENT_HANDSHAKE_BUDGET_S}s), "
        "or a held lock makes the server unreachable"
    )
