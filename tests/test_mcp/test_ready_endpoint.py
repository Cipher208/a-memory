"""Tests for /ready endpoint — alembic head check (H-T6)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_server.endpoints.system import SystemEndpoints


class _Req:
    headers: dict[str, str] = {}


@pytest.fixture
def ep() -> SystemEndpoints:
    return SystemEndpoints(None)  # type: ignore[arg-type]


def _patched_mm(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager

    mm = MigrationManager(cm=AsyncConnectionManager(base_dir=str(tmp_path)))
    monkeypatch.setattr("shared.migrations.migration_manager", mm)
    return mm


@pytest.mark.asyncio
async def test_ready_ok_after_migrate(ep: SystemEndpoints, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh migrated DB → ready=True, current==head."""
    mm = _patched_mm(tmp_path, monkeypatch)
    await mm.migrate()

    resp = await ep.ready_endpoint(_Req())  # type: ignore[arg-type]
    assert resp.status_code == 200
    body: dict[str, Any] = json.loads(resp.body)
    assert body["ready"] is True
    assert body["migration_version"]


@pytest.mark.asyncio
async def test_ready_not_ready_without_db(ep: SystemEndpoints, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """No alembic_version table → ready=False, still 200 with body."""
    _patched_mm(tmp_path, monkeypatch)

    resp = await ep.ready_endpoint(_Req())  # type: ignore[arg-type]
    assert resp.status_code == 200
    body: dict[str, Any] = json.loads(resp.body)
    assert body["ready"] is False
    assert "migration_version" in body
