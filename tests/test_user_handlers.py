"""Day-one handlers for the 7 external events (spec S3 table)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

import hooks.user_hooks as uh
from hooks.registry import HookRegistry

from shared.connection import connection_manager


@pytest.fixture
def compaction_db(tmp_path, monkeypatch):
    """Redirect base_dir to a scratch dir with the compaction_events table; restore after."""
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS compaction_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL,"
            " old_session_id TEXT,"
            " new_session_id TEXT,"
            " reason TEXT,"
            " summary TEXT,"
            " created_at REAL NOT NULL)"
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original


def test_all_seven_events_registered() -> None:
    reg = HookRegistry()
    reg.register_instance(uh.UserHooks())
    names = set(reg.list_hooks())
    for name in (
        "session_started",
        "session_ended",
        "new_message",
        "auto_save_candidate",
        "context_threshold",
        "memory_pressure",
        "post_context_compression",
    ):
        assert name in names, name


class _Mem:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, float, list[str]]] = []
        self.l1 = SimpleNamespace(get_recent=lambda n=10: [], get_full=list)
        self.l3 = SimpleNamespace(save=self._l3_save)
        self.l4 = SimpleNamespace(get_all=self._get_all)

    async def _get_all(self, user_id: str, limit: int = 50) -> list[Any]:
        return []

    async def _l3_save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        self.saved.append((user_id, summary, weight, tags))
        return 1

    async def remember(self, key: str, value: str, importance: float) -> int:
        return 2


class _Graph:
    async def add_node(self, user_id: str, content: str, node_type: str, tags: list[str], importance: float) -> int:
        return 7


@pytest.mark.asyncio
async def test_session_ended_saves_summary_to_l3() -> None:
    hooks = uh.UserHooks()
    mem = _Mem()
    result = await hooks._session_ended({"user_id": "u1", "summary": "did stuff"}, mem=mem)
    assert result["saved"] is True
    assert mem.saved[0][1] == "did stuff"


@pytest.mark.asyncio
async def test_session_ended_without_summary_skips() -> None:
    hooks = uh.UserHooks()
    result = await hooks._session_ended({"user_id": "u1"}, mem=_Mem())
    assert result["saved"] is False


@pytest.mark.asyncio
async def test_new_message_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    import hooks.external as ext

    async def fake_auto_save(mem: Any, graph: Any, user_id: str, text: str) -> dict[str, Any]:
        return {"score": 0.0, "saved_l3": False, "saved_l4": False, "saved_graph": False}

    monkeypatch.setattr(ext, "auto_save_text", fake_auto_save)
    hooks = uh.UserHooks()
    result = await hooks._new_message({"user_id": "u1", "text": "хмм"}, mem=_Mem(), graph=_Graph())
    assert result["auto_save"]["score"] == 0.0


@pytest.mark.asyncio
async def test_post_context_compression_returns_candidates(compaction_db) -> None:
    hooks = uh.UserHooks()

    class _Rag:
        async def search(self, query: str, user_id: str = "default", limit: int = 5, **kw: Any) -> list[dict[str, Any]]:
            return [{"content": "c1", "score": 0.7}]

    result = await hooks._post_context_compression({"user_id": "u1", "query": "q", "_rag": _Rag()})
    assert result["candidates"] == [{"content": "c1", "score": 0.7}]
    assert result["logged"] is True


@pytest.mark.asyncio
async def test_post_context_compression_logs_session_ids(compaction_db) -> None:
    hooks = uh.UserHooks()
    result = await hooks._post_context_compression({"user_id": "u1", "old_session_id": "s-old", "new_session_id": "s-new", "reason": "split"})
    assert result == {"candidates": [], "logged": True}
    from features.rehydrate import recent_compaction

    row = recent_compaction("u1", 1.0)
    assert row is not None
    assert row["old_session_id"] == "s-old"
    assert row["new_session_id"] == "s-new"
    assert row["reason"] == "split"


@pytest.mark.asyncio
async def test_post_context_compression_without_rag_is_safe() -> None:
    hooks = uh.UserHooks()
    result = await hooks._post_context_compression({"user_id": "u1", "query": "q"})
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_thin_advice_events() -> None:
    hooks = uh.UserHooks()
    r1 = await hooks._context_threshold({"user_id": "u1"})
    r2 = await hooks._memory_pressure({"user_id": "u1"})
    assert "advice" in r1 and "advice" in r2
