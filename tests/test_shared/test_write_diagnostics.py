"""Diagnostics on the write path: who held the lock, and for how long.

The hermes base loses the importance sweep to `database is locked` every
15 s (the busy_timeout), and the daemon dies of the same error. Until now the
journal only said "locked", so the holder was a guess. Two instruments are
missing and are built here:

* a slow write reports the CALLER's phase -- the code path that held the
  write lock long enough for another writer to lose the race;
* a busy failure reports the OS-level holders of the base's locks, read from
  /proc/locks, so the OTHER process is named instead of guessed.

Both are diagnostics only: no extra write, no changed transaction boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch

CREATE = (
    "CREATE TABLE IF NOT EXISTS core_memory ("
    "entry_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    'user_id TEXT, "key" TEXT, value TEXT, importance REAL,'
    "memory_kind TEXT, expires_at REAL, created_at REAL, updated_at REAL)"
)

INSERT = 'INSERT INTO core_memory (user_id, "key", value, importance, memory_kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)'

#: The competing writer. It takes the write lock on the WAL and holds it, so
#: the dispatch's write has to lose the race for real.
HOLDER_SCRIPT = """
import sqlite3
import sys
import time

db = sys.argv[1]
conn = sqlite3.connect(db, timeout=10, isolation_level=None)
conn.execute("PRAGMA busy_timeout=10000")
conn.execute("BEGIN IMMEDIATE")
conn.execute('INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("holder", "hk", "hv"))
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(120)
"""


@pytest.fixture
async def cm(tmp_path: Path):
    manager = AsyncConnectionManager(base_dir=str(tmp_path))
    conn = await manager.get(DB_NAME)
    await conn.execute(CREATE)
    await conn.commit()
    yield manager
    await manager.close_all()


@pytest.mark.asyncio
async def test_slow_write_names_the_callers_phase(cm, caplog, monkeypatch) -> None:
    """A write slower than the threshold reports the phase of its CALLER.

    The phase is the point of the instrument: the long holder is a code path
    inside some process, and `shared.write_dispatch` is not it. The line must
    therefore name the frame outside the dispatcher -- the caller -- and not
    the dispatcher's own frames.
    """
    from shared import write_dispatch as wd

    monkeypatch.setattr(wd, "_SLOW_WRITE_WARN_S", 0.05)
    dispatch = WriteDispatch(cm)
    # Open the dispatch's own connection first, then slow its statements down.
    conn = await dispatch._cm.get(DB_NAME)
    original_execute = conn.execute

    async def _slow(statement: str, parameters=()):
        await asyncio.sleep(0.08)
        return await original_execute(statement, parameters)

    monkeypatch.setattr(conn, "execute", _slow)

    with caplog.at_level(logging.WARNING, logger="shared.write_dispatch"):
        await dispatch.write(DB_NAME, INSERT, ("u", "k1", "v1", 0.5, "fact", 1000.0, 1000.0))

    warnings = [r.getMessage() for r in caplog.records if "slow write" in r.getMessage()]
    assert warnings, "a slow write must be reported"
    line = warnings[0]
    phase = line.split("phase=", 1)[1].split(" ", 1)[0]
    assert "test_slow_write_names_the_callers_phase" in phase, line
    assert "write_dispatch" not in phase, f"the phase must be the caller's, not the dispatcher's: {line}"
    assert "kind=write" in line, line
    assert "duration=" in line, line


@pytest.mark.asyncio
async def test_fast_write_stays_silent(cm, caplog, monkeypatch) -> None:
    """A write under the threshold says nothing at all."""
    from shared import write_dispatch as wd

    monkeypatch.setattr(wd, "_SLOW_WRITE_WARN_S", 0.05)
    dispatch = WriteDispatch(cm)

    with caplog.at_level(logging.WARNING, logger="shared.write_dispatch"):
        await dispatch.write(DB_NAME, INSERT, ("u", "k1", "v1", 0.5, "fact", 1000.0, 1000.0))

    warnings = [r.getMessage() for r in caplog.records if "slow write" in r.getMessage()]
    assert not warnings, f"a normal write must stay silent: {warnings}"


@pytest.mark.asyncio
async def test_busy_write_names_the_lock_holders(cm, caplog, monkeypatch) -> None:
    """A busy failure reports the SQLite code, the base and the OS-level holders.

    The holder is a REAL competing connection in another process: a synthetic
    log line would prove nothing about whether /proc/locks can be read and
    matched to the base's inodes.
    """
    from shared import write_dispatch as wd

    monkeypatch.setattr(wd, "_SLOW_WRITE_WARN_S", 0.05)
    dispatch = WriteDispatch(cm)
    # Open the dispatch's own connection and give it a short busy timeout so
    # the test does not wait out the production 15 s budget.
    conn = await dispatch._cm.get(DB_NAME)
    await conn.execute("PRAGMA busy_timeout=200")
    await conn.execute(CREATE)
    await conn.commit()

    script = cm.base_dir / "holder.py"
    script.write_text(HOLDER_SCRIPT, encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(cm.base_dir / DB_NAME),
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        ready = await proc.stdout.readline()
        assert ready.strip() == b"ready", f"the competing writer did not take the lock: {ready!r}"

        with caplog.at_level(logging.ERROR, logger="shared.write_dispatch"), pytest.raises(Exception) as excinfo:
            await dispatch.write(DB_NAME, INSERT, ("u", "k1", "v1", 0.5, "fact", 1000.0, 1000.0))

        assert "locked" in str(excinfo.value).lower(), excinfo.value

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "a busy failure must be reported at ERROR level"
        line = errors[0]
        assert "SQLITE_BUSY" in line, line
        assert "code=5" in line, line
        assert str(cm.base_dir) in line, f"the base directory must be named: {line}"
        assert "lock holders:" in line, line
        # The competing process is named, with its pid and its command line.
        assert f"pid={proc.pid}" in line, f"the holder's pid must be reported: {line}"
        holder_entry = next(entry for entry in line.splitlines() if f"pid={proc.pid}" in entry)
        assert "cmd=" in holder_entry, holder_entry
        assert sys.executable in holder_entry, f"the holder's command line must be reported: {holder_entry}"
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:  # pragma: no cover - defensive
            proc.kill()
            await proc.wait()


def test_lock_holders_report_no_database_when_the_base_is_empty(tmp_path: Path) -> None:
    """Nothing to attribute is said plainly, not reported as an empty list."""
    from shared.lock_owners import describe_lock_holders

    lines = describe_lock_holders(tmp_path)
    assert lines, "an unreadable state must still produce a line"
    assert "no database file" in lines[0], lines


def test_lock_holders_say_so_when_proc_locks_is_unreadable(tmp_path: Path, monkeypatch) -> None:
    """A missing /proc/locks is reported, not raised."""
    from shared import lock_owners

    db = tmp_path / DB_NAME
    db.write_bytes(b"")
    monkeypatch.setattr(lock_owners, "_PROC_LOCKS", Path("/proc/definitely-not-locks"))

    lines = lock_owners.describe_lock_holders(tmp_path)
    assert lines, "an unreadable /proc/locks must still produce a line"
    assert "could not be read" in lines[0], lines


def test_lock_holders_name_the_process_that_holds_the_write_lock(tmp_path: Path) -> None:
    """The helper itself: a real holder in this process is reported by pid."""
    import sqlite3

    from shared.lock_owners import describe_lock_holders

    db = tmp_path / DB_NAME
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(CREATE)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute('INSERT INTO core_memory (user_id, "key", value) VALUES (?, ?, ?)', ("holder", "hk", "hv"))
    try:
        lines = describe_lock_holders(tmp_path)
        assert lines, "the holder must be reported"
        assert any(f"pid={os.getpid()}" in line for line in lines), lines
        assert any("cmd=" in line for line in lines), lines
    finally:
        conn.execute("ROLLBACK")
        conn.close()
