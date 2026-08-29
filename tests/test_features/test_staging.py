"""features/staging.py — proposal lifecycle (C1.11 S4/S5)."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from features import staging
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


class _FakeMem:
    def __init__(self) -> None:
        self.remembered: list[tuple[str, str, float]] = []

    async def remember(self, key: str, value: str, importance: float) -> int:
        self.remembered.append((key, value, importance))
        return 100 + len(self.remembered)


class _FakeMM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def user_memory(self, user_id: str) -> _FakeUserMem:
        return _FakeUserMem(self.calls)


class _FakeUserMem:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    async def remember(self, key: str, value: str, importance: float) -> int:
        self._calls.append((key, value, importance))
        return 555


class _FakeApp:
    def __init__(self) -> None:
        self.mm = _FakeMM()


async def test_propose_and_list_pending(ensure_schema: Path) -> None:
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "k", "value": "v", "importance": 0.9})
    pending = await staging.list_pending("u1")
    assert len(pending) == 1
    assert pending[0]["id"] == pid
    assert pending[0]["payload"]["key"] == "k"
    assert pending[0]["status"] == "pending"


async def test_expire_stale_flips_old_pending(ensure_schema: Path) -> None:
    pid = await staging.propose("forgetting", "archive", "u1", "user", {"ids": [1, 2]})
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute("UPDATE mutation_proposals SET expires_at = ? WHERE id = ?", (time.time() - 1, pid))
    conn.commit()
    conn.close()
    expired = await staging.expire_stale()
    assert expired == 1
    assert await staging.list_pending("u1") == []


async def test_approve_core_write_calls_remember(ensure_schema: Path) -> None:
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "auto_save", "value": "важно", "importance": 0.9})
    mem = _FakeMem()
    out = await staging.decide(pid, approve=True, mem=mem)
    assert out["status"] == "applied"
    assert mem.remembered == [("auto_save", "важно", 0.9)]
    assert out["result_ref"] == "101"
    conn = sqlite3.connect(ensure_schema / "memory.db")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log").fetchall()]
    conn.close()
    assert "proposal_applied" in actions


async def test_approve_core_write_via_appcontext(ensure_schema: Path) -> None:
    """The tool path passes a full AppContext (has .mm) — resolve via user_memory."""
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "k", "value": "v", "importance": 0.9})
    out = await staging.decide(pid, approve=True, mem=_FakeApp())
    assert out["result_ref"] == "555"


async def test_reject_marks_status(ensure_schema: Path) -> None:
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "k", "value": "v", "importance": 0.9})
    out = await staging.decide(pid, approve=False, mem=_FakeMem())
    assert out["status"] == "rejected"
    assert await staging.list_pending("u1") == []


async def test_unknown_id_raises(ensure_schema: Path) -> None:
    with pytest.raises(ValueError, match="unknown proposal"):
        await staging.decide(9999, approve=True, mem=_FakeMem())


async def test_count_pending(ensure_schema: Path) -> None:
    assert await staging.count_pending("u1") == 0
    await staging.propose("auto_save", "core_write", "u1", "user", {"key": "k", "value": "v", "importance": 0.9})
    await staging.propose("auto_save", "core_write", "u1", "user", {"key": "k2", "value": "v2", "importance": 0.8})
    assert await staging.count_pending("u1") == 2
