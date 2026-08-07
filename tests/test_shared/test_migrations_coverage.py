"""Tests for shared/migrations.py — behavior tests."""

import asyncio

import pytest

from shared.migrations import MigrationManager


@pytest.fixture
def mm(tmp_path):
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    return MigrationManager(cm=cm)


def test_migrate_runs(mm):
    """migrate should run without error."""

    async def t():
        result = await mm.migrate()
        assert isinstance(result, dict)
        assert "applied" in result
        assert len(result["applied"]) > 0

    asyncio.run(t())


def test_migrate_idempotent(mm):
    """Running migrate twice should not re-apply."""

    async def t():
        r1 = await mm.migrate()
        r2 = await mm.migrate()
        assert len(r2["applied"]) == 0
        assert r2["current_version"] == r1["new_version"]

    asyncio.run(t())


def test_migrate_returns_version_info(mm):
    """migrate should return version info."""

    async def t():
        result = await mm.migrate()
        assert "current_version" in result
        assert "new_version" in result
        assert result["new_version"] >= result["current_version"]

    asyncio.run(t())
