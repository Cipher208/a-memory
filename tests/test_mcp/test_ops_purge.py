"""Tests for _purge_table target validation (SQL injection hardening)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.tools.ops import _purge_table


def _make_cm(monkeypatch):
    """The purge writes through the dispatcher, so the assertion point is its
    `write` call rather than a raw connection."""
    dispatch = MagicMock()
    dispatch.write = AsyncMock(return_value=3)
    monkeypatch.setattr("shared.write_dispatch.get_write_dispatch", lambda cm=None: dispatch)
    return MagicMock(), dispatch


@pytest.mark.asyncio
async def test_purge_table_valid(monkeypatch):
    cm, dispatch = _make_cm(monkeypatch)
    n = await _purge_table(cm, "core_memory", "u1", 123.0)
    assert n == 3
    sql = dispatch.write.call_args[0][1]
    assert sql.startswith("DELETE FROM core_memory")


@pytest.mark.asyncio
async def test_purge_table_custom_timestamp_col(monkeypatch):
    cm, dispatch = _make_cm(monkeypatch)
    n = await _purge_table(cm, "audit_log", "u1", 123.0, timestamp_col="timestamp")
    assert n == 3
    sql = dispatch.write.call_args[0][1]
    assert "timestamp" in sql


@pytest.mark.asyncio
async def test_purge_table_rejects_unknown_table(monkeypatch):
    cm, _ = _make_cm(monkeypatch)
    with pytest.raises(ValueError, match="Invalid purge target"):
        await _purge_table(cm, "sqlite_master; DROP TABLE users", "u1", 123.0)


@pytest.mark.asyncio
async def test_purge_table_rejects_unknown_timestamp_col(monkeypatch):
    cm, _ = _make_cm(monkeypatch)
    with pytest.raises(ValueError, match="Invalid purge target"):
        await _purge_table(cm, "core_memory", "u1", 123.0, timestamp_col="user_id; --")


@pytest.mark.asyncio
async def test_purge_table_none_cm():
    assert await _purge_table(None, "core_memory", "u1", 123.0) == 0
