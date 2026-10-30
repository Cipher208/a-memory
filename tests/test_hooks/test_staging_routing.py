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
    async def save(self, user_id: str, summary: str, weight: float, tags: list[str], created_at: float | None = None) -> int:
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


async def test_high_score_never_creates_proposal(ensure_schema: Path) -> None:
    """2026-09-11: auto_save staging branch removed — high-score text is routed
    by the distiller (canonical keys, dedup) and NEVER lands in the review queue."""
    from hooks.external import auto_save_text

    mem = _FakeMem()
    result = await auto_save_text(mem, _FakeGraph(), user_id="u1", text=LONG_TEXT, event="new_message")
    assert result["score"] >= 0.8, "test text must reach the L4 band"
    assert result["saved_l3"] is True and result["saved_graph"] is True
    assert not result.get("staged_l4"), "auto-save staging branch removed"
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    rows = conn.execute("SELECT kind, status, payload FROM mutation_proposals").fetchall()
    conn.close()
    assert rows == [], "auto-save must not create proposals — review queue is for deliberate mutations only"


async def test_auto_save_never_writes_raw_core_key(ensure_schema: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With staging on or off, no raw "auto_save" core-key write happens —
    the old direct-write fallback polluted one L4 slot with chat snippets."""
    import hooks.external as ext

    mem = _FakeMem()
    monkeypatch.setattr(ext, "_staging_enabled", lambda: False)
    result = await ext.auto_save_text(mem, _FakeGraph(), user_id="u1", text=LONG_TEXT, event="new_message")
    assert result["saved_l3"] is True, "distiller L3 routing unaffected"
    assert all(key != "auto_save" for key, _, _ in mem.remembered), "raw auto_save key write removed"
    assert result.get("staged_l4") is None


async def test_consolidation_empty_items_no_junk_proposal(ensure_schema: Path) -> None:
    """session_end fires consolidation with no staging_items — must stay a no-op (C1.11)."""
    from hooks.shared import consolidation

    out = await consolidation({}, "u1")
    assert "staged" not in out, "empty staging_items must not create a proposal"
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    rows = conn.execute("SELECT count(*) FROM mutation_proposals").fetchone()[0]
    conn.close()
    assert rows == 0


STAGING_ITEM = {
    "content": "кластер биллинга использует postgresql ежедневно",
    "importance": 0.9,
    "memory_kind": "fact",
}


def _proposal_layers(fresh_dir: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(fresh_dir / "memory.db")
    rows = conn.execute("SELECT layer, user_id FROM mutation_proposals ORDER BY id").fetchall()
    conn.close()
    return [(str(r[0]), str(r[1])) for r in rows]


async def test_agent_consolidation_stages_into_the_agent_queue(ensure_schema: Path) -> None:
    """F98: AgentHooks._consolidation must stage into the AGENT review queue.

    Kills the mutation that keeps `stage_consolidation(user_id, "user", ...)`
    hardcoded in `hooks.shared.consolidation` and the `layer="both"` mark that
    lets the agent handler stage on the user's queue.
    """
    from hooks.agent_hooks import AgentHooks

    out = await AgentHooks("a1")._consolidation({"staging_items": [STAGING_ITEM]})
    assert out["staged"] is True, f"setup: the item must pass the agent 0.6 gate: {out}"
    assert _proposal_layers(ensure_schema) == [("agent", "a1")]


async def test_user_consolidation_stages_into_the_user_queue(ensure_schema: Path) -> None:
    """Mirror case: the owner's consolidation keeps landing in the user queue."""
    from hooks.user_hooks import UserHooks

    out = await UserHooks("u1")._consolidation({"staging_items": [STAGING_ITEM]})
    assert out["staged"] is True, f"setup: the item must pass the user 0.7 gate: {out}"
    assert _proposal_layers(ensure_schema) == [("user", "u1")]


async def test_consolidation_defaults_to_the_user_layer(ensure_schema: Path) -> None:
    """The shared default stays "user" — scripts/verify_autohooks.py and older
    callers pass no layer and must keep staging into the user queue."""
    from hooks.shared import consolidation

    out = await consolidation({"staging_items": [STAGING_ITEM]}, "u1")
    assert out["staged"] is True, f"setup: default 0.7 gate must pass: {out}"
    assert _proposal_layers(ensure_schema) == [("user", "u1")]


async def test_each_consolidation_hook_fires_only_for_its_own_layer(ensure_schema: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F98: `layer="both"` made BOTH consolidation handlers run for every layer,
    so each event staged twice and the agent's copy landed on the user queue.

    Kills the `mark("consolidation", layer="both")` mutation on either class:
    one handler per layer, and the right one.
    """
    import hooks.agent_hooks as agent_mod
    import hooks.user_hooks as user_mod
    from hooks.registry import hook_registry

    seen: list[str] = []

    async def _user_recorder(ctx: dict, user_id: str = "default", **kwargs: object) -> dict:
        seen.append("user")
        return {"action": "recorded"}

    async def _agent_recorder(ctx: dict, user_id: str = "default", **kwargs: object) -> dict:
        seen.append("agent")
        return {"action": "recorded"}

    monkeypatch.setattr(user_mod, "consolidation", _user_recorder)
    monkeypatch.setattr(agent_mod, "consolidation", _agent_recorder)

    hook_registry.register_instance(user_mod.UserHooks("u1"))
    hook_registry.register_instance(agent_mod.AgentHooks("a1"))

    user_out = await hook_registry.fire("consolidation", "user", {"staging_items": []})
    assert user_out["handler_count"] == 1, f"only the user handler fires for the user layer: {user_out}"
    assert seen == ["user"]

    seen.clear()
    agent_out = await hook_registry.fire("consolidation", "agent", {"staging_items": []})
    assert agent_out["handler_count"] == 1, f"only the agent handler fires for the agent layer: {agent_out}"
    assert seen == ["agent"]


async def test_agent_engine_promotion_lands_in_the_agent_layer(ensure_schema: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F98, non-staging branch: with staging disabled the engine promotes directly.

    Kills the mutation `CoreMemory(cm=self._cm)` in
    `ConsolidationEngine.consolidate_staging` — the engine is constructed with a
    layer and never used it, so an agent promotion wrote the owner's L4.
    """
    from config import config
    from hooks.shared import consolidation

    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS core_memory (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT DEFAULT 'user', user_id TEXT, "key" TEXT, value TEXT,
            importance REAL, memory_kind TEXT, expires_at REAL, source TEXT,
            metadata TEXT, created_at REAL, updated_at REAL,
            visibility TEXT DEFAULT 'visible'
        );
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "get", lambda *a, **k: False if a[:2] == ("staging", "enabled") else None, raising=False)

    out = await consolidation({"staging_items": [STAGING_ITEM]}, "a1", layer="agent")
    assert out.get("promoted", 0) >= 1, f"setup: the item must pass the gate: {out}"

    conn = sqlite3.connect(ensure_schema / "memory.db")
    rows = conn.execute("SELECT layer, user_id FROM core_memory WHERE user_id='a1'").fetchall()
    conn.close()
    assert [(str(r[0]), str(r[1])) for r in rows] == [("agent", "a1")], f"agent promotion wrote the wrong layer: {rows}"
