"""One maintenance writer per base directory.

Every process serving a base starts its own `lifespan`, and `lifespan` starts
background housekeeping: `migrate()`, `backup_cron`, `importance_scheduler`, and
a 15-minute `forgetting_system.cleanup()`. Nothing coordinated them, so N
processes did the same work — and, more importantly, wrote the same SQLite file.

Measured on the live hermes base, 14.10.2026: the MCP server came up eleven times
in one day, `backup_cron` logged twice in the same minute (two instances), and
`Decay failed` went 2-4/day in September -> 14 -> 65 -> 34 by midday. The write
collisions grew the WAL to 35 MB without a checkpoint, and because `lifespan`
runs `migrate()` before the server answers, a migration that could not get the
write lock surfaced as `Failed to connect ... CancelledError` on the client —
a healthy server reported as a broken one.

`features.backup_cron` already solved this for itself and is the model here: a
non-blocking `flock` on a file next to the base, and a second holder simply does
not run (`test_backup_single_writer.py` pins that). This module gives the same
discipline to the rest of the housekeeping.

`fcntl` locks die with their process, so there is no stale-lock class to clean
up — the same reasoning `backup_cron._acquire_backup_lock` records.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path

LOCK_NAME = ".maintenance.lock"


def _lock_dir(base_dir: str | Path | None) -> Path:
    """Where the lock lives, defaulting to the manager's base directory.

    Imported lazily: `shared.connection` is heavy and this module is imported by
    test collection and by `lifespan` alike.
    """
    if base_dir is not None:
        return Path(base_dir)
    from shared.connection import connection_manager

    return Path(connection_manager.base_dir)


def acquire(base_dir: str | Path | None = None) -> int | None:
    """Take the maintenance lock for this base. A file descriptor, or None.

    Non-blocking on purpose: losing the race is the normal outcome for every
    process but the leader, and waiting would only add a second kind of stall to
    the one this exists to remove.
    """
    path = _lock_dir(base_dir) / LOCK_NAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release(fd: int | None) -> None:
    """Release a lock held by `acquire`. Safe to call with None."""
    if fd is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)
