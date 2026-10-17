"""A connection must be creatable while another process holds the write lock.

`_get_aiosqlite_conn` runs its PRAGMAs in a fixed order. One of them is the only
statement in the sequence that writes to the database:

    PRAGMA auto_vacuum=INCREMENTAL

It is not a no-op when the value is already correct — measured on a base whose
`auto_vacuum` is already 2, the statement still rewrites the header (4.8 ms on an idle
base) and therefore needs the write lock. Every other statement in the sequence passes
untouched while another connection holds a write transaction:

    faithful copy, another writer holds the file
      PRAGMA page_size=16384              ok           0.0 ms
      PRAGMA auto_vacuum=INCREMENTAL      database is locked   5006.5 ms   <- BLOCKED
      PRAGMA journal_mode=WAL             ok           0.0 ms
      PRAGMA busy_timeout=15000           ok           0.0 ms      (set AFTER it, useless)
      ... the rest                        ok           0.0 ms

5006 ms is `sqlite3.connect`'s own 5 s default, not the 15 s the code intends: the
statement that blocks runs before `busy_timeout` is applied. Moving `busy_timeout` first
does not fix it — it only makes the same failure take 15 s (measured).

What fixes it is not asking for a write that has nothing to change: `auto_vacuum` is a
property of the DATABASE, so it only needs issuing when the database does not already
have it. `PRAGMA auto_vacuum` reads it without a lock.
"""

import asyncio
import sqlite3
import time

import pytest


def _build(tmp_path, *, auto_vacuum: int) -> "object":
    """Build a base shaped like production: WAL on, auto_vacuum already applied."""
    path = tmp_path / "memory.db"
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA page_size=16384")
    c.execute(f"PRAGMA auto_vacuum={auto_vacuum}")
    c.execute("CREATE TABLE core_memory (entry_id INTEGER PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO core_memory (value) VALUES ('seed')")
    c.execute("PRAGMA journal_mode=WAL")
    c.commit()
    c.close()
    return path


@pytest.fixture
def locked_base(tmp_path):
    """A base that already has auto_vacuum=INCREMENTAL, held by another writer."""
    path = _build(tmp_path, auto_vacuum=1)  # 1 = FULL, normalised to 2 once written
    real = sqlite3.connect(str(path))
    real.execute("PRAGMA auto_vacuum=INCREMENTAL")
    real.commit()
    actual = real.execute("PRAGMA auto_vacuum").fetchone()[0]
    real.close()
    assert actual == 2, f"fixture must start with auto_vacuum=2, got {actual}"

    holder = sqlite3.connect(str(path), timeout=30)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO core_memory (value) VALUES ('held')")
    yield path
    holder.rollback()
    holder.close()


def test_a_connection_is_created_while_another_writer_holds_the_file(locked_base):
    """Starting up must not fail because a handler is mid-write somewhere else."""
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(locked_base.parent))
        try:
            t0 = time.perf_counter()
            conn = await cm.get("memory.db")
            elapsed = time.perf_counter() - t0
            assert elapsed < 3.0, f"connecting took {elapsed:.1f}s behind another writer"
            cur = await conn.execute("SELECT count(*) FROM core_memory")
            row = await cur.fetchone()
            assert row[0] == 1, "the connection must be usable, not merely open"
        finally:
            await cm.close_all()

    asyncio.run(t())


def test_auto_vacuum_is_not_rewritten_when_it_is_already_set(locked_base):
    """The header write is the blocker; a base that has it must not ask for it again."""
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(locked_base.parent))
        try:
            conn = await cm.get("memory.db")
            cur = await conn.execute("PRAGMA auto_vacuum")
            row = await cur.fetchone()
            assert row[0] == 2, "auto_vacuum must still be INCREMENTAL on the connection"
        finally:
            await cm.close_all()

    asyncio.run(t())


def test_a_new_base_still_gets_auto_vacuum(tmp_path):
    """Skipping the write when it is unnecessary must not skip it when it is needed."""
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        try:
            conn = await cm.get("fresh.db")
            await conn.execute("CREATE TABLE t (id INTEGER)")
            cur = await conn.execute("PRAGMA auto_vacuum")
            row = await cur.fetchone()
            assert row[0] == 2, "a fresh base must still be created with INCREMENTAL"
        finally:
            await cm.close_all()

    asyncio.run(t())


def test_the_connection_is_still_configured(locked_base):
    """Skipping a blocked statement must not mean skipping the settings that matter."""
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(locked_base.parent))
        try:
            conn = await cm.get("memory.db")
            cur = await conn.execute("PRAGMA busy_timeout")
            assert (await cur.fetchone())[0] > 0, "busy_timeout must be in force"
            cur = await conn.execute("PRAGMA foreign_keys")
            assert (await cur.fetchone())[0] == 1, "foreign_keys must be on"
        finally:
            await cm.close_all()

    asyncio.run(t())


def test_a_deferred_setting_is_reported_not_silent(tmp_path, caplog):
    """Deferring `journal_mode=WAL` must leave a trace.

    A base still in rollback-journal mode is the real blocking case: SQLite refuses the
    switch INSTANTLY under another writer and no timeout can help. The connection must
    still come up — and the log must say why it is not in WAL, or the next person reads
    a silent downgrade as normal operation.
    """
    import logging

    from shared.connection import AsyncConnectionManager

    path = _build(tmp_path, auto_vacuum=1)  # SQLite normalises to 2 on first write
    setup = sqlite3.connect(str(path))
    setup.execute("PRAGMA auto_vacuum=INCREMENTAL")
    setup.commit()
    # Force rollback-journal mode, so the manager has something to change.
    setup.execute("PRAGMA journal_mode=DELETE")
    mode = setup.execute("PRAGMA journal_mode").fetchone()[0]
    setup.close()
    assert mode == "delete", f"fixture must start in delete mode, got {mode}"

    holder = sqlite3.connect(str(path), timeout=30)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO core_memory (value) VALUES ('held')")
    try:

        async def t():
            cm = AsyncConnectionManager(base_dir=str(tmp_path))
            try:
                with caplog.at_level(logging.WARNING):
                    conn = await cm.get("memory.db")
                cur = await conn.execute("SELECT count(*) FROM core_memory")
                assert (await cur.fetchone())[0] >= 1, "the connection must be usable"
            finally:
                await cm.close_all()

        asyncio.run(t())
    finally:
        holder.rollback()
        holder.close()

    assert "deferred" in caplog.text.lower(), f"deferral must be logged: {caplog.text!r}"


def test_a_healthy_base_is_not_touched(caplog, tmp_path):
    """A base that already has the settings must not be written at all.

    This is what makes a locked base survivable: no write, no lock, no delay. If the code
    issued the PRAGMAs unconditionally it would either wait or log a spurious deferral on
    every single connect.
    """
    import logging

    from shared.connection import AsyncConnectionManager

    path = _build(tmp_path, auto_vacuum=1)
    setup = sqlite3.connect(str(path))
    setup.execute("PRAGMA auto_vacuum=INCREMENTAL")
    setup.commit()
    setup.execute("PRAGMA journal_mode=WAL")
    setup.commit()
    setup.close()

    holder = sqlite3.connect(str(path), timeout=30)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO core_memory (value) VALUES ('held')")
    try:

        async def t():
            cm = AsyncConnectionManager(base_dir=str(tmp_path))
            try:
                with caplog.at_level(logging.WARNING):
                    await cm.get("memory.db")
            finally:
                await cm.close_all()

        asyncio.run(t())
    finally:
        holder.rollback()
        holder.close()

    assert "deferred" not in caplog.text.lower(), f"a healthy base must not be touched: {caplog.text!r}"


def test_the_operating_timeout_is_restored_after_deferral(tmp_path):
    """Deferring must not leave the connection with the short setting wait.

    Every later statement relies on `busy_timeout`; leaving 250 ms in place would turn a
    legitimate wait into an instant failure on the next write.
    """
    from shared.connection import BUSY_TIMEOUT_MS, AsyncConnectionManager

    path = _build(tmp_path, auto_vacuum=1)
    setup = sqlite3.connect(str(path))
    setup.execute("PRAGMA auto_vacuum=INCREMENTAL")
    setup.commit()
    setup.execute("PRAGMA journal_mode=DELETE")
    setup.close()

    holder = sqlite3.connect(str(path), timeout=30)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO core_memory (value) VALUES ('held')")
    try:

        async def t():
            cm = AsyncConnectionManager(base_dir=str(tmp_path))
            try:
                conn = await cm.get("memory.db")
                cur = await conn.execute("PRAGMA busy_timeout")
                assert (await cur.fetchone())[0] == BUSY_TIMEOUT_MS
            finally:
                await cm.close_all()

        asyncio.run(t())
    finally:
        holder.rollback()
        holder.close()
