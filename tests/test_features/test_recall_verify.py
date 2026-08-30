"""D1.5 recall integration: zero-overlap semantic hits are dropped from the
recall report; verified ones stay; expand (graph) hits stay exempt."""

import pytest


class _FakeL3:
    async def search_by_tag(self, user_id, tag, limit=10):
        return []


class _FakeL4:
    async def get_all(self, user_id, limit):
        return []


class _FakeMem:
    def __init__(self):
        self.l1 = type("Recents", (), {"get_recent": staticmethod(lambda n: [])})()
        self.l3 = _FakeL3()
        self.l4 = _FakeL4()


class _FakeRag:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, user_id="default", limit=5, **kw):
        return self._hits


@pytest.mark.asyncio
async def test_recall_drops_noise_semantic_hits():
    from features.recall import recall_protocol

    rag = _FakeRag(
        [
            {"content": "uv sync ariel deploy venv", "score": 0.9, "source": "fts"},
            {"content": "совершенно посторонний текст про пироги", "score": 0.8, "source": "fts"},
        ]
    )
    blocks = await recall_protocol(_FakeMem(), rag, "u1", query="ariel deploy venv sync")
    semantic = [b for b in blocks if b["axis"] == "semantic"]
    assert len(semantic) == 1
    assert "uv sync ariel deploy venv" in semantic[0]["content"]


@pytest.mark.asyncio
async def test_recall_keeps_expand_hits_without_lexical_overlap():
    from features.recall import recall_protocol

    rag = _FakeRag(
        [
            {"content": "uv sync ariel deploy venv", "score": 0.9, "source": "fts"},
            {"content": "совершенно посторонний текст про пироги", "score": 0.4, "source": "graph_expand"},
        ]
    )
    blocks = await recall_protocol(_FakeMem(), rag, "u1", query="ariel deploy venv sync")
    axes = [b["axis"] for b in blocks]
    assert "expand" in axes
    assert all("посторонний" not in b["content"] for b in blocks if b["axis"] == "semantic")
