"""Connection health must count file descriptors, not only manager bookkeeping.

Why the obvious reading is not enough.

`connection_manager.stats()` and `leaked_connection_workers()` both report what the
ASYNC manager owns. On 16.10.2026 the hermes autohooks daemon was measured holding
**41 descriptors on one database file** (52 including -wal and -shm) while running
only **9 threads** — and 9 threads cannot back 41 aiosqlite connections, since each
one owns a worker thread. Those handles come from elsewhere.

The elsewhere is `with sqlite3.connect(...)`, which appears **42 times** in the
repository (disclosure 6, procedures 6, stash 5, scratchpad 4, door_log 3, ...). For
a `sqlite3.Connection`, `with` is a TRANSACTION context manager — it commits or rolls
back, and does NOT close. Measured directly: five such blocks left five open
descriptors, which survive until garbage collection.

So a health line built only from the manager would print `connections=1
leaked_workers=0` while the process holds 41 open files — the same shape as every
other defect in this line of work: a check that cannot see what it is checking.

This adds the descriptor count, read from /proc/self/fd, so the two numbers can be
compared. They disagreeing IS the finding.
"""

from __future__ import annotations

import sqlite3

import pytest

from shared.connection import connection_health


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Point the manager at tmp_path so the fd scan has a known directory."""
    from shared.connection import connection_manager

    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()


async def test_health_counts_open_descriptors_not_only_manager_state(base):
    """The measured defect: 41 open files, 1 managed connection, 0 leaks reported."""
    # Manager-owned connection: counted by both readings.
    from shared.connection import connection_manager

    managed = await connection_manager.get("memory.db")
    await managed.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    await managed.commit()

    # The shape the daemon has: synchronous connections opened in `with` blocks,
    # which are NOT closed by the context manager and are not tracked by the manager.
    leaked_handles = []
    for _ in range(3):
        conn = sqlite3.connect(str(base / "memory.db"))
        conn.execute("SELECT 1").fetchall()
        leaked_handles.append(conn)

    try:
        health = await connection_health()

        assert health["connections"] >= 1, health
        # NOT `leaked_workers == 0`: in the full suite another test can leave a worker
        # behind, and asserting an absolute here made this file order-dependent — the
        # same defect this session fixed elsewhere, and it failed CI on exactly that.
        # What matters is the RELATIONSHIP: the manager counts the one connection it
        # owns, while the descriptor table shows every handle this process holds.
        assert health["open_db_fds"] >= 4, f"1 managed + 3 unmanaged handles are open on this base; the reading must show them, got {health}"
        assert health["unmanaged_fds"] >= 3, f"the difference between open files and managed connections is the finding: {health}"
    finally:
        for h in leaked_handles:
            h.close()


async def test_health_is_zero_with_nothing_open(base):
    """A quiet process must read zero, not a stale or invented number."""
    health = await connection_health()

    assert health["connections"] == 0, health
    assert health["open_db_fds"] == 0, health
    assert health["unmanaged_fds"] == 0, health


async def test_health_survives_an_unreadable_directory(base, monkeypatch):
    """Diagnostics must not raise, and must not invent a zero either."""
    monkeypatch.setattr("shared.connection._proc_self_fd", None, raising=False)

    health = await connection_health()

    # -1 = could not read, which is not the same as "nothing open".
    assert health["open_db_fds"] == -1, f"an unreadable scan must be a sentinel, not a zero: {health}"
