"""Lifespan wiring: the leader starts the background set, a follower does not."""

from __future__ import annotations

import asyncio

import pytest

from mcp_server import lifespan as lifespan_mod
from mcp_server.lifespan import lifespan
from shared import leader_lock


class _Recorder:
    """Stands in for backup_cron / importance_scheduler."""

    def __init__(self) -> None:
        self.captured = False
        self.started = False
        self.stopped = False

    def capture_main_loop(self) -> None:
        self.captured = True

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def wired(monkeypatch):
    backup = _Recorder()
    scheduler = _Recorder()

    async def _migrate() -> dict:
        return {"applied": 0}

    monkeypatch.setattr(lifespan_mod, "backup_cron", backup)
    monkeypatch.setattr(lifespan_mod, "importance_scheduler", scheduler)
    monkeypatch.setattr(lifespan_mod, "AppContext", lambda: object())
    monkeypatch.setattr(lifespan_mod, "BACKGROUND_DELAY_S", 0.01)
    monkeypatch.setattr(lifespan_mod, "PROMOTION_RETRY_S", 0.05)
    monkeypatch.setattr("shared.migrations.migration_manager.migrate", _migrate)
    monkeypatch.setattr(lifespan_mod.read_only_replica, "sync", lambda: None)
    return backup, scheduler


async def test_leader_starts_background_and_releases_on_shutdown(wired) -> None:
    backup, scheduler = wired
    async with lifespan(None):  # lifespan's body never reads `server`
        await asyncio.sleep(0.1)  # claim (immediate) + BACKGROUND_DELAY_S
        assert backup.started, "the leader must start backup_cron"
        assert backup.captured, "capture_main_loop must run before start"
        assert scheduler.started, "the leader must start importance_scheduler"
    assert backup.stopped and scheduler.stopped, "shutdown must stop what it started"
    fd = leader_lock.claim()
    assert fd is not None, "shutdown must hand leadership back (leaked fd = stuck base)"
    leader_lock.release(fd)


async def test_follower_starts_no_background(wired) -> None:
    backup, scheduler = wired
    held = leader_lock.claim()
    assert held is not None, "precondition: this test is the leader"
    try:
        async with lifespan(None):
            # retry cadence is 0.05s here; 0.15s spans several failed claims
            await asyncio.sleep(0.15)
            assert not backup.started, "a follower must not run backup_cron"
            assert not scheduler.started, "a follower must not run the scheduler"
    finally:
        leader_lock.release(held)


async def test_follower_promotes_when_the_leader_leaves(wired) -> None:
    backup, _scheduler = wired
    held = leader_lock.claim()
    assert held is not None
    try:
        async with lifespan(None):
            await asyncio.sleep(0.1)
            assert not backup.started
            leader_lock.release(held)  # stand in for the old leader's process dying
            await asyncio.sleep(0.2)  # 0.05 retry + 0.01 start delay
            assert backup.started, "a follower must promote and take over the background set"
    finally:
        # Failure before the mid-context release must not leak the session lock
        # (release is a safe no-op on an already-closed fd).
        leader_lock.release(held)


async def test_shutdown_during_the_delay_never_starts_backup(wired) -> None:
    backup, _scheduler = wired
    # One loop cycle inside the context: the leadership task runs, this
    # instance promotes, and _delayed_start begins its BACKGROUND_DELAY_S
    # sleep. Exiting now lands squarely inside the delay window.
    async with lifespan(None):
        await asyncio.sleep(0)
    # state.started was True, so the finally legitimately called stop() —
    # that is NOT the race. The race is capture/start firing after shutdown.
    assert backup.stopped, "precondition: promotion happened, shutdown stopped the set"
    # Cycle well past the compressed delay (0.01s) so the orphan, if
    # unchecked, would have fired by now.
    await asyncio.sleep(0.1)
    assert not backup.captured, "capture_main_loop must not run after shutdown"
    assert not backup.started, "backup_cron must not start after shutdown"
