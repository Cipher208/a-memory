"""Tests for _purge_table target validation (SQL injection hardening)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.tools.ops import _purge_table


def _make_cm():
    cm = MagicMock()
    conn = MagicMock()
    cur = MagicMock()
    cur.rowcount = 3
    conn.execute = AsyncMock(return_value=cur)
    conn.commit = AsyncMock()
    cm.get = AsyncMock(return_value=conn)
    return cm, conn


@pytest.mark.asyncio
async def test_purge_table_valid():
    cm, conn = _make_cm()
    n = await _purge_table(cm, "core_memory", "u1", 123.0)
    assert n == 3
    sql = conn.execute.call_args[0][0]
    assert sql.startswith("DELETE FROM core_memory")


@pytest.mark.asyncio
async def test_purge_table_custom_timestamp_col():
    cm, conn = _make_cm()
    n = await _purge_table(cm, "audit_log", "u1", 123.0, timestamp_col="timestamp")
    assert n == 3
    sql = conn.execute.call_args[0][0]
    assert "timestamp" in sql


@pytest.mark.asyncio
async def test_purge_table_rejects_unknown_table():
    cm, _ = _make_cm()
    with pytest.raises(ValueError, match="Invalid purge target"):
        await _purge_table(cm, "sqlite_master; DROP TABLE users", "u1", 123.0)


@pytest.mark.asyncio
async def test_purge_table_rejects_unknown_timestamp_col():
    cm, _ = _make_cm()
    with pytest.raises(ValueError, match="Invalid purge target"):
        await _purge_table(cm, "core_memory", "u1", 123.0, timestamp_col="user_id; --")


@pytest.mark.asyncio
async def test_purge_table_none_cm():
    assert await _purge_table(None, "core_memory", "u1", 123.0) == 0
