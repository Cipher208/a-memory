"""MUSE §7.5 integration: state lifecycle events → L3 episodes (tag muse_state)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import hooks.user_hooks  # noqa: F401 — registration metadata
from hooks.external import KNOWN_EVENTS, dispatch_event
from hooks.registry import hook_registry
from hooks.user_hooks import UserHooks
from shared.connection import connection_manager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def registered_hooks():
    hook_registry.register_instance(UserHooks())
    yield
    hook_registry._hooks.pop("state_entered", None)
    hook_registry._hooks.pop("state_exited", None)


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
    import sqlite3

    conn = sqlite3.connect(fresh_dir / "memory.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'default', layer TEXT NOT NULL DEFAULT 'user',
            summary TEXT NOT NULL, weight REAL NOT NULL DEFAULT 0.5,
            tags TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memory_dispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL DEFAULT 'default',
            score REAL, saved_l3 INTEGER NOT NULL DEFAULT 0,
            saved_l4 INTEGER NOT NULL DEFAULT 0, saved_graph INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return fresh_dir


class _FakeL3:
    def __init__(self) -> None:
        self.saves: list[tuple[str, str, float, list[str]]] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        self.saves.append((user_id, summary, weight, tags))
        return 1


class _FakeGraph:
    async def add_node(self, *a: object, **k: object) -> int:
        return 1


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()

    async def remember(self, key: str, value: str, importance: float) -> int:
        return 1


def test_state_events_are_known() -> None:
    assert {"state_entered", "state_exited"} <= set(KNOWN_EVENTS)


async def test_state_entered_saves_episode(ensure_schema: Path, registered_hooks: None) -> None:
    mem = _FakeMem()
    out = await dispatch_event(
        "state_entered",
        "agent",
        "u1",
        {"state": "mommy", "intensity": 0.4, "trigger": "lily joined"},
        mem,
        _FakeGraph(),
    )
    res = out["results"][0]
    assert res["status"] == "ok" and res["state"] == "mommy"
    assert len(mem.l3.saves) == 1
    user, gist, _weight, tags = mem.l3.saves[0]
    assert user == "u1" and "mommy" in gist and "0.40" in gist
    assert "muse_state" in tags and "mommy" in tags


async def test_state_exited_carries_summary(ensure_schema: Path, registered_hooks: None) -> None:
    mem = _FakeMem()
    out = await dispatch_event(
        "state_exited",
        "agent",
        "u1",
        {"state": "3am", "duration_min": 48, "summary": "7 ideas, 2 keep"},
        mem,
        _FakeGraph(),
    )
    assert out["results"][0]["status"] == "ok"
    _, gist, _, tags = mem.l3.saves[0]
    assert "3am" in gist and "48 min" in gist and "7 ideas" in gist
    assert "muse_state" in tags


async def test_state_events_require_state(ensure_schema: Path, registered_hooks: None) -> None:
    mem = _FakeMem()
    out = await dispatch_event("state_entered", "agent", "u1", {}, mem, _FakeGraph())
    assert out["results"][0].get("skipped") == "no_state"
    assert mem.l3.saves == []


async def test_state_events_reject_unknown(ensure_schema: Path, registered_hooks: None) -> None:
    with pytest.raises(ValueError, match="unknown event"):
        await dispatch_event("state_rotated", "agent", "u1", {}, _FakeMem(), _FakeGraph())
