"""Who runs the background set: one leader per base.

Every process serving a base starts its own `lifespan`, and before this module
each one planted the full garden — `backup_cron`, `importance_scheduler` and
the 15-minute housekeeping loop (spec S1). `shared.maintenance_lock` already
gives one writer per *pass*; this file gives one *member* per base: the
instance that holds this lock runs the background set for as long as it lives.

Deliberately a different file from `.maintenance.lock`, and deliberately not
held for the process lifetime through that module: the leader's own scheduler
and backup passes need the turnstile (they acquire it per pass), and on Linux
flock conflicts even between two fds of one process — a lifetime hold there
would deadlock the holder against itself.

`fcntl` locks die with their process, so a crashed leader frees leadership
immediately: there is no stale-leader class to clean up. See
`shared/maintenance_lock.py` for the same reasoning on its side.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path

LOCK_NAME = ".leader.lock"


def _lock_dir(base_dir: str | Path | None) -> Path:
    """Where the lock lives, defaulting to the manager's base directory.

    Imported lazily: `shared.connection` is heavy and this module is imported
    by test collection and by `lifespan` alike.
    """
    if base_dir is not None:
        return Path(base_dir)
    from shared.connection import connection_manager

    return Path(connection_manager.base_dir)


def claim(base_dir: str | Path | None = None) -> int | None:
    """Take leadership of this base. A file descriptor, or None.

    Non-blocking on purpose: losing the race is the normal state of a
    follower (spec S4) — the caller retries on its own cadence
    (`lifespan.PROMOTION_RETRY_S`), so waiting here would only add a second
    kind of stall to the one this exists to remove.
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
    """Release leadership taken by `claim`. Safe to call with None."""
    if fd is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)
