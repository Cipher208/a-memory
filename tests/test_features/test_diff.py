"""Pure gap computation (C1.10 S5)."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from features.diff import compute_session_gaps
from shared.connection import connection_manager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()


def _seed_log(tmp_path: Path, rows: list[tuple]) -> None:
    """rows: (event, source_msg_id, user_id, score, saved_l3, saved_l4, saved_graph, created_at)"""
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.executescript("""
        CREATE TABLE memory_dispatch_log (
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
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO memory_dispatch_log (event, source_msg_id, user_id, score, saved_l3, saved_l4, saved_graph, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


class _FakeL3:
    def get(self, msg_id: int) -> dict:
        return {"content": f"preview for {msg_id}"}


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()


def test_high_score_l3_only_is_gap_missing_l4(fresh_dir: Path) -> None:
    """score 0.6, saved_l3=1, saved_l4=0 → gap with missing=['l4']."""
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.6, 1, 0, 1, time.time())])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1)
    assert len(gaps) == 1
    g = gaps[0]
    assert g["missing"] == ["l4"]
    assert "preview for 1" in g["text_preview"]


def test_high_score_full_save_is_not_a_gap(fresh_dir: Path) -> None:
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.9, 1, 1, 1, time.time())])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1)
    assert gaps == []


def test_empty_log_yields_no_gaps(fresh_dir: Path) -> None:
    _seed_log(fresh_dir, [])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1)
    assert gaps == []


def test_no_saves_at_all_is_a_gap(fresh_dir: Path) -> None:
    """score 0.6 with saved_l3=0, saved_l4=0 → gap with missing=['l3','l4']."""
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.6, 0, 0, 0, time.time())])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1)
    assert len(gaps) == 1
    assert set(gaps[0]["missing"]) == {"l3", "l4"}


def test_other_events_ignored(fresh_dir: Path) -> None:
    _seed_log(fresh_dir, [("session_started", None, "u1", 0.9, 1, 1, 1, time.time())])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1)
    assert gaps == []


def test_time_window_filters_old_log_rows(fresh_dir: Path) -> None:
    old = time.time() - 7200
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.6, 1, 0, 1, old)])
    gaps = compute_session_gaps(_FakeMem(), since=time.time() - 60, until=time.time() + 1)
    assert gaps == []
