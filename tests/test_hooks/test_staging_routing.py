"""Risk-tier routing: L4 writes and destructive ops become proposals (C1.11 S3)."""

from __future__ import annotations

import sqlite3
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
        CREATE TABLE IF NOT EXISTS memory_dispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL DEFAULT 'default',
            score REAL, saved_l3 INTEGER NOT NULL DEFAULT 0,
            saved_l4 INTEGER NOT NULL DEFAULT 0, saved_graph INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            action TEXT NOT NULL, layer TEXT, target_id TEXT, details TEXT, timestamp REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return fresh_dir


class _FakeL3:
    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        return 1


class _FakeGraph:
    async def add_node(self, user_id: str, value: str, node_type: str, tags: list[str], weight: float) -> int:
        return 1


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()
        self.remembered: list[tuple[str, str, float]] = []

    async def remember(self, key: str, value: str, importance: float) -> int:
        self.remembered.append((key, value, importance))
        return 1


LONG_TEXT = (
    "Запомни важное: я переделал архитектуру памяти?\n"
    "Это поэтапный план, который надо срочно сделать!\n"
    "Сначала — прототип, потом надо починить баг и выпустить релиз.\n"
    "Иначе всё сломается — ты же помнишь, как я решил?"
)


async def test_high_score_creates_proposal_not_l4(ensure_schema: Path) -> None:
    from hooks.external import auto_save_text

    mem = _FakeMem()
    result = await auto_save_text(mem, _FakeGraph(), user_id="u1", text=LONG_TEXT, event="new_message")
    assert result["score"] >= 0.8, "test text must reach the L4 band"
    assert result["saved_l3"] is True and result["saved_graph"] is True
    assert result.get("staged_l4") is True
    assert mem.remembered == [], "L4 write must be deferred to proposal apply"
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    rows = conn.execute("SELECT kind, status, payload FROM mutation_proposals").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "core_write" and rows[0][1] == "pending"
    assert '"importance"' in rows[0][2]


async def test_staging_disabled_writes_l4_directly(ensure_schema: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hooks.external as ext

    mem = _FakeMem()
    monkeypatch.setattr(ext, "_staging_enabled", lambda: False)
    result = await ext.auto_save_text(mem, _FakeGraph(), user_id="u1", text=LONG_TEXT, event="new_message")
    assert result["saved_l4"] is True
    assert mem.remembered, "staging disabled → direct L4 write"
    assert result.get("staged_l4") is None
