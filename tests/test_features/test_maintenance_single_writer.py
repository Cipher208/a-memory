"""One maintenance writer per base: a second process must stand down.

Why this test exists rather than a careful diagnosis.

Every process that serves the same base starts its own `lifespan`, and each
lifespan starts the same background work. Measured on the live hermes base on
14.10.2026: the MCP server alone came up **eleven times in one day**, and
`features.backup_cron` logged twice in the same minute — two instances, each
running `backup_cron`, `importance_scheduler` and its own 15-minute
`forgetting_system.cleanup()`.

The damage is not the duplicated work, it is the duplicated *writers*. SQLite
gives one writer at a time, so `BEGIN IMMEDIATE` began timing out
(`database is locked`): `Decay failed` went 2-4/day in September -> 14 on 12.10
-> 65 on 13.10 -> 34 by midday on 14.10, and the WAL grew to 35 MB without ever
checkpointing. Worse, `lifespan` runs `migrate()` **before** the server answers
anything, so a migration that could not get the write lock left the client with
`Failed to connect ... CancelledError` while the server itself was healthy —
the exact shape of the 07:02/08:02 session failures.

`backup_cron` already solved this shape for itself: it holds a non-blocking
`flock` on `<base_dir>/.backup_cron.lock` and a second holder simply does not
run. `test_backup_single_writer.py` pins that behaviour. This file asks the same
of the rest of the housekeeping, through one shared lock.
"""

from __future__ import annotations

import asyncio
import fcntl

from lifecycle.importance_scheduler import ImportanceScheduler, SchedulerConfig
from shared.maintenance_lock import LOCK_NAME, acquire, release


def _hold_for_another_process(tmp_path):
    """Stand in for a second process holding the maintenance lock."""
    # Held open on purpose: closing the file would release the flock, and the
    # whole point is that the lock stays taken while the test runs.
    fh = open(tmp_path / LOCK_NAME, "w")  # noqa: SIM115
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def test_second_holder_gets_nothing(tmp_path) -> None:
    first = acquire(tmp_path)
    assert first is not None
    assert acquire(tmp_path) is None, "second holder must not receive the lock"
    release(first)
    assert acquire(tmp_path) is not None, "lock must be reusable after release"


def test_lock_file_is_named_after_what_it_protects(tmp_path) -> None:
    # The name is part of the contract: a lock at a surprise path would let two
    # processes protect different things and both believe they were the leader.
    fd = acquire(tmp_path)
    try:
        assert (tmp_path / LOCK_NAME).exists()
    finally:
        release(fd)


def test_scheduler_stands_down_while_another_process_leads(tmp_path, monkeypatch) -> None:
    """A sweep that cannot claim the write lock must do nothing at all.

    The defect is not that a stray process starts a thread, it is that the
    sweep then *writes* the same base as the leader. The lock is taken around
    the work, not around the process, because `lifespan` runs two kinds of
    housekeeping (this sweep and the 15-minute cleanup) and they have to take
    turns rather than exclude each other for the process's lifetime.
    """
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager.base_dir", tmp_path, raising=False)
    calls = 0

    async def _counted(self) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"rescored": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr(ImportanceScheduler, "_rescore_pass", _counted, raising=True)
    held = _hold_for_another_process(tmp_path)
    try:
        scheduler = ImportanceScheduler(scheduler_config=SchedulerConfig(interval_seconds=1))
        stats = asyncio.run(scheduler.run_once())
        assert calls == 0, "the sweep ran while another process held the write lock"
        assert stats == {"rescored": 0, "skipped": 0, "errors": 0}
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()


def test_scheduler_runs_when_it_is_the_leader(tmp_path, monkeypatch) -> None:
    """The other half: the guard must not disable housekeeping outright."""
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager.base_dir", tmp_path, raising=False)
    calls = 0

    async def _counted(self) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"rescored": 3, "skipped": 0, "errors": 0}

    monkeypatch.setattr(ImportanceScheduler, "_rescore_pass", _counted, raising=True)
    scheduler = ImportanceScheduler(scheduler_config=SchedulerConfig(interval_seconds=1))
    stats = asyncio.run(scheduler.run_once())
    assert calls == 1, "the leader still has to do the work"
    assert stats["rescored"] == 3
    # And the lock is handed back: a released lock is what lets the next process
    # — or a later process after a crash — take over.
    assert acquire(tmp_path) is not None
