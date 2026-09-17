"""Recall must surface query-relevant L3 episodes (Elli 16.09 D3).

Semantic recall searches wiki-only rag_pages; recent session work lives in
episodes and was invisible (persona/leak work never surfaced).
"""

import time
from types import SimpleNamespace

import pytest

from shared.connection import connection_manager


class _FakeL3Search:
    """L3 fake WITH EpisodicMemory.search (tokenized LIKE, newest-first)."""

    def __init__(self, episodes):
        self._episodes = episodes

    async def search_by_tag(self, user_id, tag, limit=10):
        return []

    async def search(self, user_id, query, limit=10):
        toks = [w.lower() for w in query.split() if w]
        out = [e for e in self._episodes if any(t in e.summary.lower() for t in toks)]
        return sorted(out, key=lambda e: e.created_at, reverse=True)[:limit]


class _FakeL3Old:
    """Legacy L3 fake WITHOUT .search — must degrade, not crash."""

    async def search_by_tag(self, user_id, tag, limit=10):
        return []


async def _no_facts(user_id, limit):
    return []


def _mem(l3):
    return SimpleNamespace(
        l1=SimpleNamespace(get_recent=lambda n: []),
        l3=l3,
        l4=SimpleNamespace(get_all=_no_facts),
    )


@pytest.fixture
def tmp_base(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    connection_manager._conns.clear()
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    yield tmp_path
    connection_manager.base_dir = original
    connection_manager._conns.clear()


@pytest.mark.asyncio
async def test_episode_axis_surfaces_recent_work(tmp_base):
    from features.recall import recall_protocol

    now = time.time()
    mem = _mem(
        _FakeL3Search(
            [
                SimpleNamespace(
                    summary="Personas CowAgent memory isolation second leak analysis",
                    created_at=now - 86400,
                ),
                SimpleNamespace(summary="picoclaw router notes", created_at=now - 60 * 86400),
            ]
        )
    )
    blocks = await recall_protocol(mem, None, "default", query="personas CowAgent memory isolation", budget=2000)
    eps = [b for b in blocks if b["axis"] == "episodes"]
    assert len(eps) == 1
    assert "Personas" in eps[0]["content"]
    assert "picoclaw" not in eps[0]["content"]


@pytest.mark.asyncio
async def test_no_search_method_degrades(tmp_base):
    """Old mem fakes without l3.search must not break recall."""
    from features.recall import recall_protocol

    blocks = await recall_protocol(_mem(_FakeL3Old()), None, "default", query="anything here", budget=2000)
    assert isinstance(blocks, list)
