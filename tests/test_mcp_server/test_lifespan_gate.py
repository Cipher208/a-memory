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
