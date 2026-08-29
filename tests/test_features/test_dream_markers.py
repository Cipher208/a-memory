"""DREAM: marker detection + staged routing (C1.12 S3)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from features.importance import detect_dream_marker
from shared.connection import connection_manager

if TYPE_CHECKING:
    from pathlib import Path


def test_detect_memory_target() -> None:
    m = detect_dream_marker("DREAM: memory: я решил перейти на новый стек")
    assert m == {"target": "memory", "content": "я решил перейти на новый стек"}


def test_detect_fact_case_insensitive() -> None:
    m = detect_dream_marker("drem ignored\nDream: Fact: сервер vm1282045")
    assert m is not None
    assert m["target"] == "fact"


def test_detect_skill_target() -> None:
    m = detect_dream_marker("DREAM: skill: деплой через restic + borg")
    assert m == {"target": "skill", "content": "деплой через restic + borg"}


def test_no_marker_returns_none() -> None:
    assert detect_dream_marker("обычное сообщение без маркеров") is None


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
        CREATE TABLE IF NOT EXISTS memory_dispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL DEFAULT 'default',
            score REAL, saved_l3 INTEGER NOT NULL DEFAULT 0, saved_l4 INTEGER NOT NULL DEFAULT 0,
            saved_graph INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
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
        return 1


class _FakeGraph:
    async def add_node(self, *a, **k) -> int:
        return 1


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()
        self.remembered: list[tuple[str, str, float]] = []

    async def remember(self, key: str, value: str, importance: float) -> int:
        self.remembered.append((key, value, importance))
        return 1


async def test_marker_routes_to_staged_proposal(ensure_schema: Path) -> None:
    from hooks.external import auto_save_text

    mem = _FakeMem()
    result = await auto_save_text(mem, _FakeGraph(), user_id="u1", text="DREAM: memory: важное решение о стеке")
    assert result.get("dream", {}).get("staged") is True
    assert mem.remembered == [], "marker goes through staging, not direct write"
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    rows = conn.execute("SELECT source, kind, payload FROM mutation_proposals").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "dream" and rows[0][1] == "core_write"
    assert '"importance": 0.95' in rows[0][2]


async def test_dream_markers_toggle_disabled_falls_to_heuristics(ensure_schema: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """staging.dream_markers=false → marker text takes the regular heuristic path."""
    import hooks.external as ext

    monkeypatch.setattr(ext, "_dream_markers_enabled", lambda: False)
    mem = _FakeMem()
    result = await ext.auto_save_text(mem, _FakeGraph(), user_id="u1", text="DREAM: memory: важное решение о стеке")
    assert "dream" not in result
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    rows = conn.execute("SELECT count(*) FROM mutation_proposals").fetchone()[0]
    conn.close()
    assert rows == 0


async def test_skill_marker_also_writes_l3_episode(ensure_schema: Path) -> None:
    from hooks.external import auto_save_text

    mem = _FakeMem()
    await auto_save_text(mem, _FakeGraph(), user_id="u1", text="DREAM: skill: деплой через restic")
    assert mem.l3.saved, "skill marker writes a dream_skill L3 episode"
    assert mem.l3.saved[0][3] == ["dream_skill"]
