"""Tests for the unified search() facade on RAGEngine."""

import warnings

import pytest

from rag.engine import RAGEngine, StrategyT
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def rag(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    r = RAGEngine(cm=cm, layer="test_facade", binary_dim=8)
    await r.init_db()
    return r


@pytest.fixture
async def rag_with_data(rag):
    await rag.ingest_text("Python Guide", "Python is great for AI and machine learning", user_id="u1")
    await rag.ingest_text("Redis Tuning", "Redis is configured for high throughput with pipelining", user_id="u1")
    await rag.ingest_text("CSS Styles", "Buttons use CSS with hover transitions and rounded corners", user_id="u1")
    return rag


class TestStrategyType:
    def test_strategy_literal_valid(self):
        strategies: list[StrategyT] = ["fts", "mib", "hybrid", "auto"]
        assert all(s in ("fts", "mib", "hybrid", "auto") for s in strategies)


class TestUnifiedSearch:
    @pytest.mark.asyncio
    async def test_search_defaults_to_fts(self, rag_with_data):
        results = await rag_with_data.search("Python", user_id="u1")
        assert len(results) > 0
        assert any("Python" in r["title"] or "python" in r["content"].lower() for r in results)

    @pytest.mark.asyncio
    async def test_search_explicit_fts(self, rag_with_data):
        results = await rag_with_data.search("Redis", user_id="u1", strategy="fts")
        assert len(results) > 0
        assert any("Redis" in r["title"] for r in results)

    @pytest.mark.asyncio
    async def test_search_mib_strategy(self, rag_with_data):
        results = await rag_with_data.search("Redis performance", user_id="u1", strategy="mib")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_hybrid_strategy(self, rag_with_data):
        results = await rag_with_data.search("Redis cluster", user_id="u1", strategy="hybrid")
        assert isinstance(results, list)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_search_auto_short_query_uses_fts(self, rag_with_data):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            results = await rag_with_data.search("Python", user_id="u1", strategy="auto")
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_search_auto_long_query_uses_hybrid(self, rag_with_data):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            results = await rag_with_data.search("Redis high throughput configuration", user_id="u1", strategy="auto")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_unknown_strategy_raises(self, rag_with_data):
        with pytest.raises(ValueError, match="unknown strategy"):
            await rag_with_data.search("test", user_id="u1", strategy="bogus")

    @pytest.mark.asyncio
    async def test_search_respects_limit(self, rag_with_data):
        results = await rag_with_data.search("content", user_id="u1", strategy="fts", limit=1)
        assert len(results) <= 1

    @pytest.mark.asyncio
    async def test_search_empty_database(self, rag):
        results = await rag.search("anything", user_id="empty")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_user_filtering(self, rag):
        await rag.ingest_text("Alice Doc", "Content for alice", user_id="alice")
        await rag.ingest_text("Bob Doc", "Content for bob", user_id="bob")
        alice_results = await rag.search("content", user_id="alice", strategy="fts")
        bob_results = await rag.search("content", user_id="bob", strategy="fts")
        assert len(alice_results) == 1
        assert len(bob_results) == 1
        assert "alice" in alice_results[0]["content"].lower()
        assert "bob" in bob_results[0]["content"].lower()


class TestAutoStrategy:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("python", "fts"),
            ("redis cluster", "fts"),
            ("redis high throughput", "hybrid"),
            ("", "fts"),
        ],
    )
    def test_auto_strategy(self, rag, query, expected):
        from rag.search import auto_strategy

        assert auto_strategy(query) == expected


class TestSearchStrategyInit:
    @pytest.mark.asyncio
    async def test_default_strategy_is_fts(self, rag):
        assert rag.search_strategy == "fts"

    @pytest.mark.asyncio
    async def test_custom_strategy(self, tmp_path):
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        r = RAGEngine(cm=cm, layer="test_custom", search_strategy="hybrid")
        assert r.search_strategy == "hybrid"


class TestMaterializeCandidates:
    def test_deduplicates_by_id(self, rag):
        from rag.search import materialize_candidates

        results = [
            {"id": 1, "title": "A", "content": "text", "wiki_type": None, "score": 0.8, "source": "fts5"},
            {"id": 1, "title": "A", "content": "text", "wiki_type": None, "score": 0.9, "source": "mib"},
        ]
        candidates = materialize_candidates(results)
        assert len(candidates) == 1
        assert candidates[0].rrf_score == 0.9
        assert candidates[0].bin_score == 0.9

    def test_merge_scores(self, rag):
        from rag.search import materialize_candidates

        results = [
            {"id": 1, "title": "A", "content": "text", "wiki_type": None, "score": 0.5, "source": "fts5"},
            {"id": 2, "title": "B", "content": "text", "wiki_type": None, "score": 0.7, "source": "mib"},
        ]
        candidates = materialize_candidates(results)
        assert len(candidates) == 2


class TestFormatResult:
    def test_truncates_long_content(self, rag):
        from rag.scoring import ScoredCandidate
        from rag.search import format_result

        c = ScoredCandidate(id=1, page_id=1, title="T", content="x" * 600, wiki_type=None, rrf_score=0.5)
        result = format_result(c)
        assert result["content"].endswith("...")
        assert len(result["content"]) == 503

    def test_preserves_short_content(self, rag):
        from rag.scoring import ScoredCandidate
        from rag.search import format_result

        c = ScoredCandidate(id=1, page_id=1, title="T", content="short", wiki_type=None, rrf_score=0.5)
        result = format_result(c)
        assert result["content"] == "short"
