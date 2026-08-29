"""memory_proposals tool (C1.11 S5)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from shared.connection import connection_manager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


@pytest.fixture()
def ensure_schema(fresh_dir: Path) -> Path:
    conn = sqlite3.connect(fresh_dir / "memory.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mutation_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, kind TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default', layer TEXT NOT NULL DEFAULT 'user',
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            proposed_at REAL NOT NULL, expires_at REAL NOT NULL,
            decided_at REAL, decided_by TEXT, result_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            action TEXT NOT NULL, layer TEXT, target_id TEXT, details TEXT, timestamp REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return fresh_dir


class _Ctx:
    def __init__(self) -> None:
        self.request_context = SimpleNamespace(lifespan_context=None)


async def _call(ensure_schema: Path, **kwargs):
    from mcp_server.tools.ops import memory_proposals

    return await memory_proposals(ctx=_Ctx(), **kwargs)


async def test_list_returns_pending(ensure_schema: Path) -> None:
    from features.staging import propose

    await propose("auto_save", "core_write", "default", "user", {"key": "k", "value": "v", "importance": 0.9})
    out = await _call(ensure_schema, action="list")
    assert out["status"] == "ok"
    assert len(out["proposals"]) == 1
    assert out["proposals"][0]["kind"] == "core_write"


async def test_unknown_action_raises(ensure_schema: Path) -> None:
    with pytest.raises(ValueError, match="unknown action"):
        await _call(ensure_schema, action="bogus")


async def test_unknown_id_raises(ensure_schema: Path) -> None:
    with pytest.raises(ValueError, match="unknown proposal"):
        await _call(ensure_schema, action="decide", proposal_id=424242, approve=True)
