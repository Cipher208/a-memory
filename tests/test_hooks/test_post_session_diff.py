"""post_session_diff auto-handler materializes gaps as diff_gap L3 episodes (C1.10 S4)."""

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
        CREATE TABLE memory_dispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL DEFAULT 'default',
            score REAL,
            saved_l3 INTEGER NOT NULL DEFAULT 0,
            saved_l4 INTEGER NOT NULL DEFAULT 0,
            saved_graph INTEGER NOT NULL DEFAULT 0,
            text_preview TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return fresh_dir


class _FakeL3:
    def __init__(self) -> None:
        self.layer = "user"
        self.saved: list[dict] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str], created_at: float | None = None) -> int:
        self.saved.append({"user_id": user_id, "summary": summary, "weight": weight, "tags": tags})
        return len(self.saved)


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()


def _seed_log(tmp_path: Path, rows: list[tuple]) -> None:
    """rows: (event, source_msg_id, user_id, score, saved_l3, saved_l4, saved_graph, created_at[, preview])"""
    conn = sqlite3.connect(tmp_path / "memory.db")
    for r in rows:
        if len(r) == 8:
            r = (*r, "seeded message preview")
        conn.execute(
            "INSERT INTO memory_dispatch_log (event, source_msg_id, user_id, score, saved_l3, saved_l4, saved_graph, created_at, text_preview) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


async def test_post_session_diff_handler_materializes_gap(ensure_schema: Path) -> None:
    """A real gap (score above the L4 bar) becomes a tagged L3 episode.

    The score is 0.9 on purpose: the L4 bar is `l4_min_importance` (0.8), and a row in
    the 0.5-0.8 band is normal L3-only routing rather than a gap.
    """
    _seed_log(ensure_schema, [("new_message", 7, "u1", 0.9, 1, 0, 1, time.time(), "выбрали postgres 16")])
    from mcp_server.context import AppContext

    app = AppContext()
    uh = app.user_hooks
    mem = _FakeMem()
    result = await uh._post_session_diff({"user_id": "u1", "since": 0, "until": time.time() + 1}, mem=mem)
    assert result["gaps"] == 1
    assert mem.l3.saved[0]["user_id"] == "u1"
    assert "diff_gap" in mem.l3.saved[0]["tags"]
    assert "auto_review" in mem.l3.saved[0]["tags"]
    assert "msg=7" in mem.l3.saved[0]["summary"]
    assert "выбрали postgres 16" in mem.l3.saved[0]["summary"]


async def test_post_session_diff_skips_empty_preview(ensure_schema: Path) -> None:
    """Empty-preview rows are noise, not gaps (13k-marker regression, 2026-09-12).

    Score 0.9 so the rows DO meet the L4 bar and the preview rule is what excludes them —
    at 0.6 they would be excluded by the threshold instead and this test would prove
    nothing about previews.
    """
    _seed_log(
        ensure_schema,
        [
            ("new_message", 7, "u1", 0.9, 1, 0, 1, time.time(), ""),
            ("new_message", 8, "u1", 0.9, 1, 0, 1, time.time(), "   "),
        ],
    )
    from mcp_server.context import AppContext

    app = AppContext()
    uh = app.user_hooks
    mem = _FakeMem()
    result = await uh._post_session_diff({"user_id": "u1", "since": 0, "until": time.time() + 1}, mem=mem)
    assert result["gaps"] == 0
    assert mem.l3.saved == []


async def test_post_session_diff_ignores_the_band_below_the_l4_bar(ensure_schema: Path) -> None:
    """The 0.5-0.8 band is L3-only routing: no episode, no marker.

    This is the end-to-end half of the 2026-10-12 fix — 71% of the accumulated
    `diff_gap` episodes came from it, and the owner read them as an open backlog.
    """
    _seed_log(ensure_schema, [("new_message", 7, "u1", 0.6, 1, 0, 1, time.time(), "обычный разговор")])
    from mcp_server.context import AppContext

    app = AppContext()
    uh = app.user_hooks
    result = await uh._post_session_diff({"user_id": "u1", "since": 0, "until": time.time() + 1}, mem=_FakeMem())
    assert result["gaps"] == 0


async def test_post_session_diff_with_no_log_yields_zero_gaps(ensure_schema: Path) -> None:
    _seed_log(ensure_schema, [])
    from mcp_server.context import AppContext

    app = AppContext()
    uh = app.user_hooks
    mem = _FakeMem()
    result = await uh._post_session_diff({"user_id": "u1", "since": 0, "until": time.time() + 1}, mem=mem)
    assert result["gaps"] == 0
    assert mem.l3.saved == []


async def test_post_session_diff_without_mem_returns_zero(ensure_schema: Path) -> None:
    from mcp_server.context import AppContext

    app = AppContext()
    uh = app.user_hooks
    result = await uh._post_session_diff({"user_id": "u1", "since": 0, "until": time.time() + 1}, mem=None)
    assert result["gaps"] == 0
    assert result["skipped"] == "no_mem"
