"""auto_save_text writes one memory_dispatch_log row per save (C1.10 S3)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from shared.connection import connection_manager

if TYPE_CHECKING:
    from pathlib import Path


LONG_TEXT = (
    "Запомни важное: я переделал архитектуру памяти?\n"
    "Это поэтапный план, который надо надо сделать!\n"
    "Сначала — прототип, потом надо починить баг и выпустить релиз.\n"
    "Иначе всё сломается — ты же помнишь, как я решил?"
)  # len > 100, has ?, has !, has keyword, 2+ newlines → ~0.85 score

BELOW_TEXT = "ок"  # len < 20 → score 0


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    """Redirect ariel's connection_manager to a tmp dir for one test."""
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


@pytest.fixture()
def ensure_schema(fresh_dir: Path) -> Path:
    """Create the dispatch log table (C1.10 migration) against the tmp dir."""
    conn = sqlite3.connect(fresh_dir / "memory.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_dispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL DEFAULT 'default',
            score REAL,
            saved_l3 INTEGER NOT NULL DEFAULT 0,
            saved_l4 INTEGER NOT NULL DEFAULT 0,
            saved_graph INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mutation_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, kind TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default', layer TEXT NOT NULL DEFAULT 'user',
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            proposed_at REAL NOT NULL, expires_at REAL NOT NULL,
            decided_at REAL, decided_by TEXT, result_ref TEXT
        );
    """)
    conn.commit()
    conn.close()
    return fresh_dir


class _FakeL3:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, float, list[str]]] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        self.saved.append((user_id, summary, weight, tags))
        return len(self.saved)


class _FakeL4:
    def __init__(self) -> None:
        self.remembered: list[tuple[str, str, float]] = []

    async def remember(self, key: str, value: str, importance: float) -> int:
        self.remembered.append((key, value, importance))
        return len(self.remembered)


class _FakeGraph:
    def __init__(self) -> None:
        self.added: list[tuple[str, str, list[str], float]] = []

    async def add_node(self, user_id: str, value: str, node_type: str, tags: list[str], weight: float) -> int:
        self.added.append((user_id, value, node_type, tags, weight))
        return len(self.added)


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()
        self.l4 = _FakeL4()

    async def remember(self, key: str, value: str, importance: float) -> int:
        return await self.l4.remember(key, value, importance)


async def test_high_score_writes_log_row_with_saved_l3_and_graph(ensure_schema: Path) -> None:
    from hooks.external import auto_save_text

    mem = _FakeMem()
    graph = _FakeGraph()
    result = await auto_save_text(
        mem,
        graph,
        user_id="u1",
        text=LONG_TEXT,
        event="new_message",
        source_msg_id=42,
    )
    assert result["score"] >= 0.5
    assert result["saved_l3"] is True
    assert result["saved_graph"] is True
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    rows = conn.execute("SELECT event, source_msg_id, user_id, score, saved_l3, saved_l4, saved_graph FROM memory_dispatch_log").fetchall()
    conn.close()
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "new_message"
    assert r[1] == 42
    assert r[2] == "u1"
    assert r[3] >= 0.5
    assert r[4] == 1 and r[6] == 1


async def test_below_threshold_writes_no_log_row(ensure_schema: Path) -> None:
    from hooks.external import auto_save_text

    mem = _FakeMem()
    graph = _FakeGraph()
    result = await auto_save_text(mem, graph, user_id="u1", text=BELOW_TEXT, event="new_message")
    assert result["saved_l3"] is False
    assert result["saved_l4"] is False
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    count = conn.execute("SELECT count(*) FROM memory_dispatch_log").fetchone()[0]
    conn.close()
    assert count == 0


async def test_log_insert_failure_does_not_break_save(ensure_schema: Path) -> None:
    """If the log table is missing, auto_save_text must still return a successful save result."""
    from hooks.external import auto_save_text

    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    conn.execute("DROP TABLE memory_dispatch_log")
    conn.commit()
    conn.close()

    mem = _FakeMem()
    graph = _FakeGraph()
    result = await auto_save_text(mem, graph, user_id="u1", text=LONG_TEXT, event="new_message")
    assert result["saved_l3"] is True
    assert mem.l3.saved, "l3.save must have been called even when the log insert failed"
