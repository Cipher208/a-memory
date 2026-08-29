"""Revert of applied proposals (C1.13 S4)."""

from __future__ import annotations

import sqlite3
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, kind TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default', layer TEXT NOT NULL DEFAULT 'user',
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            proposed_at REAL NOT NULL, expires_at REAL NOT NULL,
            decided_at REAL, decided_by TEXT, result_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            action TEXT NOT NULL, layer TEXT, target_id TEXT, details TEXT, timestamp REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS core_memory (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            importance REAL DEFAULT 0.5, is_conflict INTEGER DEFAULT 0,
            conflict_group_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            memory_kind TEXT, expires_at REAL, source TEXT DEFAULT 'manual', metadata TEXT
        );
        CREATE TABLE IF NOT EXISTS archived_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL DEFAULT 'default',
            original_id INTEGER, content TEXT NOT NULL, memory_type TEXT, importance REAL,
            archive_reason TEXT NOT NULL, archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memory_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, kind TEXT NOT NULL,
            from_ref TEXT NOT NULL, to_ref TEXT NOT NULL, reason TEXT, ts REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return fresh_dir


class _FakeUserMem:
    def __init__(self, store: dict) -> None:
        self._store = store

    async def remember(self, key: str, value: str, importance: float) -> int:
        self._store[key] = value
        return 900

    async def forget(self, key: str) -> bool:
        return self._store.pop(key, None) is not None


class _FakeMM:
    def __init__(self, store: dict) -> None:
        self._store = store

    def user_memory(self, user_id: str) -> _FakeUserMem:
        return _FakeUserMem(self._store)


class _FakeApp:
    def __init__(self) -> None:
        self.mm = _FakeMM({"dream_memory_0": "важное решение"})


async def test_revert_core_write_deletes_entry(ensure_schema: Path) -> None:
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "dream_memory_0", "value": "важное решение", "importance": 0.9})
    applied = await staging.decide(pid, True, mem=_FakeApp())
    assert applied["status"] == "applied"
    out = await staging.revert(pid, mem=_FakeApp())
    assert out["status"] == "reverted"
    conn = sqlite3.connect(ensure_schema / "memory.db")
    status = conn.execute("SELECT status FROM mutation_proposals WHERE id = ?", (pid,)).fetchone()[0]
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log").fetchall()]
    conn.close()
    assert status == "reverted"
    assert "proposal_reverted" in actions


async def test_revert_archive_restores_rows(ensure_schema: Path) -> None:
    conn = sqlite3.connect(ensure_schema / "memory.db")
    now = 1000.0
    conn.execute(
        "INSERT INTO core_memory (user_id, key, value, importance, created_at, updated_at, memory_kind)"
        " VALUES ('u1', 'old_fact', 'старый факт', 0.3, ?, ?, 'fact')",
        (now, now),
    )
    entry_id = conn.execute("SELECT entry_id FROM core_memory WHERE key='old_fact'").fetchone()[0]
    conn.execute(
        "INSERT INTO archived_memories (user_id, original_id, content, memory_type, importance, archive_reason)"
        " VALUES ('u1', ?, 'old_fact=старый факт', 'fact', 0.3, 'inactive_90d')",
        (entry_id,),
    )
    conn.execute("DELETE FROM core_memory WHERE entry_id = ?", (entry_id,))
    conn.commit()
    conn.close()

    pid = await staging.propose("forgetting", "archive", "u1", "user", {"ids": [entry_id]})
    await staging.decide(pid, True, mem=_FakeApp())
    out = await staging.revert(pid, mem=_FakeApp())
    assert out["status"] == "reverted"
    conn = sqlite3.connect(ensure_schema / "memory.db")
    restored = conn.execute("SELECT count(*) FROM core_memory WHERE key='old_fact'").fetchone()[0]
    archived_left = conn.execute("SELECT count(*) FROM archived_memories WHERE original_id=?", (entry_id,)).fetchone()[0]
    conn.close()
    assert restored == 1 and archived_left == 0


async def test_revert_rejects_non_applied(ensure_schema: Path) -> None:
    pid = await staging.propose("auto_save", "core_write", "u1", "user", {"key": "k", "value": "v", "importance": 0.9})
    with pytest.raises(ValueError, match="not applied"):
        await staging.revert(pid, mem=_FakeApp())


async def test_revert_rejects_consolidate_kind(ensure_schema: Path) -> None:
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute(
        "INSERT INTO mutation_proposals (source, kind, user_id, layer, payload, status, proposed_at, expires_at, decided_at, decided_by, result_ref)"
        " VALUES ('consolidation', 'consolidate_staging', 'u1', 'user', '{}', 'applied', 1, 2, 3, 'tool', 'promoted=4')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="revert not supported"):
        await staging.revert(1, mem=_FakeApp())
