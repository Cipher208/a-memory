"""Tests for shared/migrations.py — behavior tests."""

import asyncio

import pytest

from shared.migrations import MigrationManager


@pytest.fixture
def mm(tmp_path):
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    return MigrationManager(cm=cm)


@pytest.mark.asyncio
async def test_migrate_runs(mm):
    """migrate should run without error."""
    result = await mm.migrate()
    assert isinstance(result, dict)
    assert result["status"] in ("up_to_date", "initialized")
    # In a fresh DB, new_version should be the latest head
    assert result["new_version"] is not None


@pytest.mark.asyncio
async def test_migrate_idempotent(mm):
    """Running migrate twice should not re-apply."""
    r1 = await mm.migrate()
    r2 = await mm.migrate()
    # In idempotent run, new_version should be same as previous new_version
    assert r2["current_version"] == r1["new_version"]
    assert r2["new_version"] == r1["new_version"]


@pytest.mark.asyncio
async def test_migrate_returns_version_info(mm):
    """migrate should return version info."""
    result = await mm.migrate()
    assert "current_version" in result
    assert "new_version" in result
    # For a fresh migrate, new_version is the head, current_version was None
    assert result["new_version"] is not None
