"""memory_report_card rollup (C1.14 S5)."""

from __future__ import annotations

import sqlite3
import time
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
    now = time.time()
    conn.execute(
        "INSERT INTO mutation_proposals (source, kind, user_id, layer, payload, status, proposed_at, expires_at, decided_at, decided_by, result_ref)"
        " VALUES ('auto_save', 'core_write', 'default', 'user', '{}', 'applied', ?, ?, ?, 'tool', '900')",
        (now - 60, now + 86400, now - 30),
    )
    conn.execute(
        "INSERT INTO mutation_proposals (source, kind, user_id, layer, payload, status, proposed_at, expires_at)"
        " VALUES ('dream', 'core_write', 'default', 'user', '{}', 'pending', ?, ?)",
        (now - 30, now + 86400),
    )
    conn.execute(
        "INSERT INTO memory_dispatch_log (event, source_msg_id, user_id, score, saved_l3, saved_l4, saved_graph, created_at)"
        " VALUES ('new_message', 1, 'default', 0.6, 1, 0, 1, ?)",
        (now - 120,),
    )
    conn.commit()
    conn.close()
    return fresh_dir


class _Ctx:
    def __init__(self) -> None:
        self.request_context = SimpleNamespace(lifespan_context=None)


async def test_report_card_counts(ensure_schema: Path) -> None:
    from mcp_server.tools.ops import memory_report_card

    out = await memory_report_card(period_hours=24, ctx=_Ctx())
    assert out["status"] == "ok"
    assert out["proposals"]["created"] == 2
    assert out["proposals"]["applied"] == 1
    assert out["proposals"]["pending"] >= 1
    assert out["dream_markers"] == 1
    assert out["auto_save"]["dispatched"] == 1
    assert out["auto_save"]["saved_l3"] == 1
    assert out["gaps"]["count"] == 1


async def test_report_card_empty_db_zeroed(tmp_path: Path) -> None:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    try:
        from mcp_server.tools.ops import memory_report_card

        out = await memory_report_card(period_hours=24, ctx=_Ctx())
        assert out["proposals"]["created"] == 0
        assert out["auto_save"]["dispatched"] == 0
        assert out["gaps"]["count"] == 0
    finally:
        connection_manager._conns.clear()
        connection_manager.base_dir = original


def test_review_tier_exposure() -> None:
    from mcp_server.server import resolve_exposure

    names = {"memory_proposals", "memory_report_card", "think", "memory_stats"}
    exposed = resolve_exposure("primitives,review", names)
    assert {"memory_proposals", "memory_report_card", "think"} <= exposed
    assert "memory_stats" not in exposed
    assert resolve_exposure("primitives", names) == {"think"}
