"""Tests for shared/connection.py — remaining unit tests."""

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _uid():
    return uuid.uuid4().hex[:8]


def test_connection_rollback():
    """rollback() should undo uncommitted changes."""
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir="/tmp/test_conn")
        conn = await cm.get(f"rollback_{_uid()}.db")
        await conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        await conn.execute("INSERT INTO t VALUES (1)")
        await conn.rollback()
        cur = await conn.execute("SELECT COUNT(*) FROM t")
        row = await cur.fetchone()
        assert row[0] == 0

    asyncio.run(t())


def test_new_db_pragmas(tmp_path):
    """A2.6: fresh DB gets page_size=16384 + auto_vacuum=INCREMENTAL."""
    import asyncio

    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        conn = await cm.get(f"pragmas_{_uid()}.db")
        cur = await conn.execute("PRAGMA page_size")
        assert (await cur.fetchone())[0] == 16384
        cur = await conn.execute("PRAGMA auto_vacuum")
        assert (await cur.fetchone())[0] == 2  # INCREMENTAL

    asyncio.run(t())


def test_connection_stale_reopen():
    """Stale connection should be reopened automatically."""
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir="/tmp/test_conn")
        name = f"stale_{_uid()}.db"
        conn1 = await cm.get(name)
        await conn1.close()
        cm._conns.pop(name, None)
        conn2 = await cm.get(name)
        assert conn2 is not None
        cur = await conn2.execute("SELECT 1")
        row = await cur.fetchone()
        assert row[0] == 1

    asyncio.run(t())
