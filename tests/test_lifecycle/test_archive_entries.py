"""archive_entries pins explicit ids (C1.11 S4)."""

from __future__ import annotations

import sqlite3
import time
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
        CREATE TABLE IF NOT EXISTS core_memory (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            importance REAL DEFAULT 0.5, is_conflict INTEGER DEFAULT 0,
            conflict_group_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            memory_kind TEXT, expires_at REAL, source TEXT DEFAULT 'manual', metadata TEXT
        );
        CREATE TABLE IF NOT EXISTS archived_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, content TEXT,
            memory_type TEXT, importance REAL, original_id INTEGER, reason TEXT, archived_at REAL
        );
    """)
    now = time.time()
    conn.executemany(
        "INSERT INTO core_memory (user_id, key, value, importance, created_at, updated_at, memory_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("u1", f"k{i}", f"v{i}", 0.3, now - 400 * 86400, now, "fact") for i in range(1, 4)],
    )
    conn.commit()
    conn.close()
    return fresh_dir


async def test_archive_entries_archives_only_pinned_ids(ensure_schema: Path) -> None:
    from lifecycle.forgetting import ForgettingSystem

    fs = ForgettingSystem(layer="user")
    count = await fs.archive_entries([1, 2])
    assert count == 2
    conn = sqlite3.connect(ensure_schema / "memory.db")
    remaining = [r[0] for r in conn.execute("SELECT entry_id FROM core_memory").fetchall()]
    archived = conn.execute("SELECT count(*) FROM archived_memories").fetchone()[0]
    conn.close()
    assert remaining == [3]
    assert archived == 2


async def test_archive_entries_with_missing_ids_is_best_effort(ensure_schema: Path) -> None:
    from lifecycle.forgetting import ForgettingSystem

    fs = ForgettingSystem(layer="user")
    count = await fs.archive_entries([2, 999])
    assert count == 1
