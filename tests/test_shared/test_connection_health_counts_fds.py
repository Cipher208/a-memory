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
back, and does NOT close. Measured directly: fifty such blocks left **fifty-four** open
descriptors on one file, and holding a reference to each one left 117.

So a health line built only from the manager would print `connections=1
leaked_workers=0` while the process holds 41 open files — the same shape as every
other defect in this line of work: a check that cannot see what it is checking.

This adds the descriptor count, read from /proc/self/fd, so the two numbers can be
compared. They disagreeing IS the finding.
"""

from __future__ import annotations

import os
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


async def test_wal_and_shm_handles_are_not_counted_as_database_descriptors(base):
    """A healthy WAL connection must not read as a leak.

    Measured on this base before the rule was narrowed: one managed connection, whose
    WAL mode legitimately holds three descriptors (`db`, `-wal`, `-shm`), counted as
    three open databases against one managed connection, so `unmanaged_fds` read 2 on a
    process that was behaving perfectly — and it can never read 0 that way, which makes
    the finding it exists to surface unusable. The journal describes the database; it is
    not a second database.
    """
    from shared.connection import connection_manager

    managed = await connection_manager.get("memory.db")
    await managed.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    await managed.execute("INSERT INTO t VALUES ('x')")
    await managed.commit()

    health = await connection_health()

    assert health["connections"] == 1, health
    assert health["open_db_fds"] == 1, f"one database file is open; -wal and -shm are not databases: {health}"
    assert health["unmanaged_fds"] == 0, f"a healthy connection must not read as unmanaged handles: {health}"


async def test_a_deleted_database_file_is_still_counted_while_open(base):
    """A replaced database keeps its descriptor, and is still a database held open.

    Rotating a database is done by writing a new file and unlinking the old one; the
    open descriptor then reads `<path> (deleted)`. Counting it is the honest answer —
    the process does hold a database file open — so the suffix must not disqualify it.
    """
    path = base / "rotated.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.commit()
    os.unlink(path)

    try:
        targets = []
        for entry in os.listdir("/proc/self/fd"):
            try:
                targets.append(os.readlink(f"/proc/self/fd/{entry}"))
            except OSError:
                continue
        assert any(t.endswith("(deleted)") for t in targets), f"the fixture must really show a deleted file: {targets}"

        health = await connection_health()

        assert health["open_db_fds"] == 1, f"a deleted-but-open database is still open: {health}"
    finally:
        conn.close()


async def test_databases_outside_the_base_directory_are_not_counted(base, tmp_path_factory):
    """The reading is for THIS base, not for every database the process happens to hold.

    Without the guard the count would include other houses' bases, the context plugin's
    database and any scratch file left open anywhere on the machine — a number that
    moves for reasons this base cannot explain.
    """
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    conn = sqlite3.connect(str(elsewhere / "foreign.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.commit()

    try:
        health = await connection_health()

        assert health["open_db_fds"] == 0, f"a database outside the base is not this base's reading: {health}"
        assert health["unmanaged_fds"] == 0, health
    finally:
        conn.close()


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
