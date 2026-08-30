"""D1.1: /recall protocol tests — axes, proportionality, dedupe, budget."""

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


def _ep(summary, age_s=0.0, ts=None):
    return SimpleNamespace(summary=summary, created_at=ts or (time.time() - age_s))


@pytest.fixture
def compaction_free(tmp_path, monkeypatch):
    """Isolate base_dir (SessionStore reads memory.db); restore after."""
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    yield tmp_path
    connection_manager.base_dir = original


@pytest.mark.asyncio
async def test_full_protocol_axes_order(compaction_free):
    from features.recall import recall_protocol

    mem = _FakeMem(
        l1=[SimpleNamespace(role="user", content="session chatter", timestamp=time.time())],
        by_tag={
            "dream_skill": [_ep("dream skill episode")],
            "auto_save": [_ep("day digest fact")],
        },
        l4=[SimpleNamespace(key="dream_memory_1", value="durable intent", importance=0.95)],
    )
    rag = _FakeRag([
        {"content": "semantic hit", "score": 0.8, "source": "fts"},
        {"content": "graph neighbor", "score": 0.4, "source": "graph_expand"},
    ])
    blocks = await recall_protocol(mem, rag, "u1", query="find durable intent")
    axes = [b["axis"] for b in blocks]
    assert axes == ["markers", "session", "semantic", "expand", "day"]
    assert "durable intent" in blocks[0]["content"]
    assert "graph neighbor" in blocks[3]["content"]


@pytest.mark.asyncio
async def test_zero_state_proportional(compaction_free):
    from features.recall import recall_protocol

    mem = _FakeMem(
        by_tag={"auto_save": [_ep("day digest fact")]},
        l4=[SimpleNamespace(key="dream_memory_1", value="durable intent", importance=0.95)],
    )
    blocks = await recall_protocol(mem, _FakeRag([]), "u1", query="")
    axes = [b["axis"] for b in blocks]
    assert axes == ["markers", "day"]
    assert all(b["axis"] != "semantic" for b in blocks)


@pytest.mark.asyncio
async def test_markers_outrank_and_dedupe(compaction_free):
    from features.recall import recall_protocol

    mem = _FakeMem(
        l4=[SimpleNamespace(key="dream_fact_9", value="shared content here", importance=0.95)],
    )
    rag = _FakeRag([{"content": "shared content here", "score": 0.9, "source": "fts"}])
    blocks = await recall_protocol(mem, rag, "u1", query="shared")
    axes = [b["axis"] for b in blocks]
    assert axes.count("markers") == 1
    assert "semantic" not in axes  # deduped: markers saw the content first


@pytest.mark.asyncio
async def test_budget_skips_oversized_blocks(compaction_free):
    from features.recall import recall_protocol

    mem = _FakeMem(
        l4=[SimpleNamespace(key="dream_memory_1", value="x" * 5000, importance=0.95)],
    )
    blocks = await recall_protocol(_FakeMem(), _FakeRag([]), "u1", query="", budget=100)
    assert blocks == []


@pytest.mark.asyncio
async def test_stale_day_episodes_excluded(compaction_free):
    from features.recall import recall_protocol

    mem = _FakeMem(by_tag={"auto_save": [_ep("old", age_s=3 * 86400), _ep("fresh")]})
    blocks = await recall_protocol(mem, _FakeRag([]), "u1", query="")
    day = next(b for b in blocks if b["axis"] == "day")
    assert "fresh" in day["content"] and "old" not in day["content"]
