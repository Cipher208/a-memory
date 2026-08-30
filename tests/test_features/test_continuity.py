"""D1.2 session_recap — /new recovery pack over sessions + pending + zero-state tail."""

import time
from types import SimpleNamespace

import pytest

from shared.connection import connection_manager


class _FakeL3:
    def __init__(self, by_tag=None):
        self._by_tag = by_tag or {}

    async def search_by_tag(self, user_id, tag, limit=10):
        return self._by_tag.get(tag, [])


class _FakeL4:
    def __init__(self, facts=()):
        self._facts = list(facts)

    async def get_all(self, user_id, limit):
        return self._facts


class _FakeMem:
    def __init__(self, by_tag=None, l4=()):
        self.l1 = SimpleNamespace(get_recent=lambda n: [])
        self.l3 = _FakeL3(by_tag)
        self.l4 = _FakeL4(l4)


def _ep(summary, age_s=0.0):
    return SimpleNamespace(summary=summary, created_at=time.time() - age_s)


@pytest.fixture
async def compaction_free(tmp_path, monkeypatch):
    """Isolate base_dir (SessionStore + scratchpad read memory.db); restore after.

    close_all() on both sides: connection_manager caches aiosqlite conns by
    DB_NAME key, not full path — without the flush another test's tmp-dir
    connection leaks in (manifests as a foreign recap_session block).
    """
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    await connection_manager.close_all()
    yield tmp_path
    connection_manager.base_dir = original
    await connection_manager.close_all()


@pytest.mark.asyncio
async def test_recap_session_axis(compaction_free):
    from core.session import SessionStore
    from features.continuity import session_recap

    store = SessionStore()
    await store._init_db()
    sid = await store.create_session("u1")
    await store.close_session(sid, summary="deployed a-memory v1.9", topics=["release", "wiki"], state_deltas={"version": "1.9.0"}, message_count=12)
    blocks = await session_recap(_FakeMem(), "u1")
    assert blocks, f"res={blocks}"
    assert blocks[0]["axis"] == "recap_session"
    assert "deployed a-memory v1.9" in blocks[0]["content"]
    assert "release" in blocks[0]["content"]
    assert "changes: version" in blocks[0]["content"]


@pytest.mark.asyncio
async def test_recap_open_session_skipped(compaction_free):
    from core.session import SessionStore
    from features.continuity import session_recap

    store = SessionStore()
    await store._init_db()
    await store.create_session("u1")  # never closed → not a recap source
    blocks = await session_recap(_FakeMem(), "u1")
    assert all(b["axis"] != "recap_session" for b in blocks), f"res={blocks}"


@pytest.mark.asyncio
async def test_recap_pending_scratchpad(compaction_free):
    from features.continuity import session_recap
    from features.scratchpad import write_entry

    await write_entry("u1", "user", "hypothesis", "alembic revision before DDL")
    blocks = await session_recap(_FakeMem(), "u1")
    pending = next(b for b in blocks if b["axis"] == "recap_pending")
    assert "pad:hypothesis=" in pending["content"]
    assert "alembic revision" in pending["content"]


@pytest.mark.asyncio
async def test_recap_counts_staged_proposals(compaction_free, monkeypatch):
    from features import continuity as cont

    async def _fake_pending(user_id="default", limit=20):
        return [{"id": 1}, {"id": 2}]

    monkeypatch.setattr(cont, "_pending_proposals", _fake_pending)
    from features.continuity import session_recap

    blocks = await session_recap(_FakeMem(), "u1")
    pending = next(b for b in blocks if b["axis"] == "recap_pending")
    assert "staged proposals: 2" in pending["content"]


@pytest.mark.asyncio
async def test_recap_zero_state_tail(compaction_free):
    from features.continuity import session_recap

    mem = _FakeMem(
        by_tag={"auto_save": [_ep("day digest fact")]},
        l4=[SimpleNamespace(key="dream_fact_1", value="durable", importance=0.95)],
    )
    blocks = await session_recap(mem, "u1")
    assert [b["axis"] for b in blocks] == ["markers", "day"]
    assert "durable" in blocks[0]["content"]
    assert "day digest fact" in blocks[1]["content"]


@pytest.mark.asyncio
async def test_recap_budget_skips_oversized(compaction_free):
    from features.continuity import session_recap
    from features.scratchpad import write_entry

    await write_entry("u1", "user", "big", "x" * 5000)
    blocks = await session_recap(_FakeMem(), "u1", budget=10)
    assert blocks == []
