"""memory_watch operator tool (C1.10 S6)."""

from __future__ import annotations

import json
import sqlite3
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


def _create_watch_rules(tmp_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.executescript("""
        CREATE TABLE watch_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            trigger TEXT NOT NULL,
            predicate TEXT NOT NULL,
            action TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
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
            "INSERT INTO watch_rules (name, trigger, predicate, action, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


class _Ctx:
    """Minimal ctx shape so _get_ctx() returns its lifespan_context (None is fine)."""

    def __init__(self) -> None:
        self.request_context = SimpleNamespace(lifespan_context=None)


async def _call(fresh_dir: Path, **kwargs):
    from mcp_server.tools.ops import memory_watch

    return await memory_watch(ctx=_Ctx(), **kwargs)


async def test_list_returns_seeded_rules(fresh_dir: Path) -> None:
    _create_watch_rules(
        fresh_dir,
        [("auto_save_default", "new_message", '{"min_importance": 0.5}', "auto_save_text", 1, 0.0)],
    )
    out = await _call(fresh_dir, action="list")
    assert out["status"] == "ok"
    assert any(r["name"] == "auto_save_default" for r in out["rules"])


async def test_add_with_unknown_predicate_key_raises(fresh_dir: Path) -> None:
    _create_watch_rules(fresh_dir, [])
    with pytest.raises(ValueError, match="predicate key"):
        await _call(
            fresh_dir,
            action="add",
            name="r1",
            trigger="new_message",
            predicate_json=json.dumps({"min_importance": 0.5, "exotic": 1}),
            action_kind="auto_save_text",
        )


async def test_add_with_duplicate_name_raises(fresh_dir: Path) -> None:
    _create_watch_rules(
        fresh_dir,
        [("auto_save_default", "new_message", '{"min_importance": 0.5}', "auto_save_text", 1, 0.0)],
    )
    with pytest.raises(sqlite3.IntegrityError):
        await _call(
            fresh_dir,
            action="add",
            name="auto_save_default",
            trigger="new_message",
            predicate_json=json.dumps({"min_importance": 0.5}),
            action_kind="auto_save_text",
        )


async def test_add_then_disable_then_list(fresh_dir: Path) -> None:
    _create_watch_rules(fresh_dir, [])
    added = await _call(
        fresh_dir,
        action="add",
        name="r1",
        trigger="new_message",
        predicate_json=json.dumps({"min_importance": 0.6}),
        action_kind="auto_save_text",
    )
    assert added["status"] == "ok"
    rule_id = added["id"]
    out = await _call(fresh_dir, action="disable", rule_id=rule_id)
    assert out["status"] == "ok"
    out = await _call(fresh_dir, action="list", enabled_only=True)
    assert all(r["name"] != "r1" for r in out["rules"])


async def test_delete_removes_row(fresh_dir: Path) -> None:
    _create_watch_rules(
        fresh_dir,
        [("r1", "new_message", '{"min_importance": 0.5}', "auto_save_text", 1, 0.0)],
    )
    out = await _call(fresh_dir, action="delete", rule_id=1)
    assert out["status"] == "ok"
    out = await _call(fresh_dir, action="list")
    assert out["rules"] == []
