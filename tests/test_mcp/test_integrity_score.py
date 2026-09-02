"""E5: verify results land in audit_log; report card computes 100*survived/total."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from shared.connection import connection_manager

if TYPE_CHECKING:
    from collections.abc import Iterator

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Iterator[Path]:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


class _Ctx:
    def __init__(self) -> None:
        self.request_context = SimpleNamespace(lifespan_context=None)


def _seed_audit_log(db_path: Path, rows: list[tuple[int, int, float]]) -> None:
    conn = sqlite3.connect(db_path)
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
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, action TEXT NOT NULL,
            layer TEXT, target_id TEXT, details TEXT, timestamp REAL NOT NULL
        );
    """)
    now = time.time()
    for verified, dropped, age_s in rows:
        conn.execute(
            "INSERT INTO audit_log (user_id, action, layer, details, timestamp) VALUES (?, 'verify_result', 'user', ?, ?)",
            ("default", f'{{"verified": {verified}, "dropped": {dropped}}}', now - age_s),
        )
    conn.commit()
    conn.close()


async def test_report_card_integrity_score(fresh_dir: Path) -> None:
    _seed_audit_log(fresh_dir / "memory.db", [(8, 2, 60), (5, 1, 120)])
    from mcp_server.tools.ops import memory_report_card

    card = await memory_report_card(period_hours=24, ctx=_Ctx())
    assert card["integrity"]["verified"] == 13
    assert card["integrity"]["dropped"] == 3
    assert card["integrity"]["score"] == pytest.approx(81.3, abs=0.1)


async def test_report_card_integrity_empty_window(fresh_dir: Path) -> None:
    _seed_audit_log(fresh_dir / "memory.db", [])
    from mcp_server.tools.ops import memory_report_card

    card = await memory_report_card(period_hours=24, ctx=_Ctx())
    assert card["integrity"] == {"score": None, "verified": 0, "dropped": 0}


async def test_report_card_integrity_no_db(tmp_path: Path) -> None:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    try:
        from mcp_server.tools.ops import memory_report_card

        card = await memory_report_card(period_hours=24, ctx=_Ctx())
        assert card["integrity"] == {"score": None, "verified": 0, "dropped": 0}
    finally:
        connection_manager._conns.clear()
        connection_manager.base_dir = original


async def test_recall_logs_verify_result(fresh_dir: Path) -> None:
    """recall_protocol() writes the verify aggregate into audit_log."""
    from features.audit_trail import AuditTrail
    from features.recall import recall_protocol

    trail = AuditTrail()
    await trail._init_db()

    class _Hits:
        async def search(self, query, user_id=None, limit=8):
            return [
                {"content": "postgresql tuning guide", "score": 0.9},
                {"content": "zzzqqq unhit noise", "score": 0.8},
            ]

    class _L1:
        def get_recent(self, n):
            return []

    class _L3:
        async def search_by_tag(self, user_id, tag, limit):
            return []

    class _L4:
        async def get_all(self, user_id, limit):
            return []

    mem = SimpleNamespace(l1=_L1(), l3=_L3(), l4=_L4())
    blocks = await recall_protocol(mem, _Hits(), "default", query="postgresql tuning", budget=2000)
    assert any(b["axis"] == "semantic" for b in blocks)
    rows = await trail.get_history("default", action="verify_result")
    assert rows, "recall must log the verify aggregate"
    assert rows[0]["details"]["verified"] == 1
    assert rows[0]["details"]["dropped"] == 1
