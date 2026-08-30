"""D1.10: smart context budget — weighted allocation across memory sources."""

import time
from types import SimpleNamespace

import pytest

from shared.connection import connection_manager


class _FakeL1:
    def __init__(self, entries):
        self._entries = entries

    def get_recent(self, n):
        return self._entries


class _FakeL3:
    def __init__(self, by_tag):
        self._by_tag = by_tag

    async def search_by_tag(self, user_id, tag, limit=10):
        return self._by_tag.get(tag, [])


class _FakeL4:
    def __init__(self, facts):
        self._facts = facts

    async def get_all(self, user_id, limit):
        return self._facts


class _FakeMem:
    def __init__(self, l1=(), by_tag=None, l4=()):
        self.l1 = _FakeL1(list(l1))
        self.l3 = _FakeL3(by_tag or {})
        self.l4 = _FakeL4(list(l4))


class _FakeRag:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, user_id="default", limit=5, **kw):
        return self._hits


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    yield tmp_path
    connection_manager.base_dir = original


@pytest.mark.asyncio
async def test_weighted_allocation_gives_each_source_a_floor(isolated_db):
    """A fat important-facts list must not starve recent/day sources."""
    from features.smart_context import build_smart_context

    fat = [SimpleNamespace(key=f"k{i}", value="v" * 200, importance=0.9) for i in range(10)]
    mem = _FakeMem(
        l1=[SimpleNamespace(role="user", content="recent chatter", timestamp=time.time())],
        by_tag={"auto_save": [SimpleNamespace(summary="day fact", created_at=time.time())]},
        l4=fat,
    )
    out = await build_smart_context(mem, _FakeRag([]), "u1", query="", budget=400)
    axes = {b["source"] for b in out["blocks"]}
    assert "important" in axes and "recent" in axes and "day" in axes
    used_important = sum(
        b["tokens"] for b in out["blocks"] if b["source"] == "important"
    )
    # floor 0.30*400=120, ceiling 2x floor=240: the fat list may not take everything
    assert used_important <= 245
    assert used_important >= 100  # but it does get its floor's worth


@pytest.mark.asyncio
async def test_leftover_redistributes_to_starved_sources(isolated_db):
    """Unfilled sources hand their budget to the ones with more content."""
    from features.smart_context import build_smart_context

    mem = _FakeMem(
        l1=[SimpleNamespace(role="user", content="tiny", timestamp=time.time())],
        by_tag={"auto_save": [SimpleNamespace(summary="d" * 300, created_at=time.time())]},
        l4=[SimpleNamespace(key="k", value="v" * 300, importance=0.9)],
    )
    out = await build_smart_context(mem, _FakeRag([]), "u1", query="", budget=300)
    total = sum(b["tokens"] for b in out["blocks"])
    # the fat day entry (75 tokens > floor 45) made it in via redistribution
    assert out["allocations"]["day"]["used"] >= 70
    assert out["allocations"]["relevant"]["used"] == 0  # no query → no RAG hits
    assert total <= 300


@pytest.mark.asyncio
async def test_relevant_axis_needs_query(isolated_db):
    from features.smart_context import build_smart_context

    mem = _FakeMem(l4=[SimpleNamespace(key="k", value="v", importance=0.9)])
    out = await build_smart_context(mem, _FakeRag([{"content": "hit", "score": 0.8}]), "u1", query="", budget=500)
    assert all(b["source"] != "relevant" for b in out["blocks"])
    out2 = await build_smart_context(mem, _FakeRag([{"content": "hit", "score": 0.8}]), "u1", query="hit", budget=500)
    assert any(b["source"] == "relevant" for b in out2["blocks"])


@pytest.mark.asyncio
async def test_total_budget_never_exceeded(isolated_db):
    from features.smart_context import build_smart_context

    fat = [SimpleNamespace(key=f"k{i}", value="v" * 150, importance=0.9) for i in range(6)]
    mem = _FakeMem(
        l1=[SimpleNamespace(role="user", content="c" * 150, timestamp=time.time())],
        by_tag={"auto_save": [SimpleNamespace(summary="d" * 150, created_at=time.time())]},
        l4=fat,
    )
    out = await build_smart_context(mem, _FakeRag([]), "u1", query="", budget=200)
    assert sum(b["tokens"] for b in out["blocks"]) <= 200
