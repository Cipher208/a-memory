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
