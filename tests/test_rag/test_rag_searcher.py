import pytest
from unittest.mock import AsyncMock, MagicMock
from rag.searcher import RAGSearcher
from rag.models import SearchResult
from shared.importance import ImportanceScorer, ScorerResult, ImportanceSignals


@pytest.fixture
def mock_cm():
    cm = MagicMock()
    cm.get = AsyncMock()
    return cm


@pytest.fixture
def mock_scorer():
    scorer = MagicMock(spec=ImportanceScorer)
    # Mock score method to return a predictable result
    scorer.score.return_value = ScorerResult(
        score=0.8,
        signals=ImportanceSignals(
            base=1.0, length=1.0, question=0.0, tech_keyword=0.0, emotional=0.0, novelty=0.0, retrieval_signal=0.0, noise_penalty=0.0
        ),
    )
    return scorer


@pytest.mark.asyncio
async def test_search_fts5_mock(mock_cm):
    searcher = RAGSearcher(mock_cm, layer="user")

    # Mock the internal _search_fts5 to avoid DB calls in this unit test
    searcher._search_fts5 = AsyncMock(return_value=[SearchResult(page_id=1, title="Test", content="Content", score=0.5)])

    results = await searcher.search("query", strategy="fts")
    assert len(results) == 1
    assert results[0].title == "Test"
    searcher._search_fts5.assert_called_once()


@pytest.mark.asyncio
async def test_search_hybrid_with_scorer(mock_cm, mock_scorer):
    searcher = RAGSearcher(mock_cm, layer="user", scorer=mock_scorer)

    # Mock internal searches
    searcher._search_fts5 = AsyncMock(return_value=[SearchResult(page_id=1, title="FTS Doc", content="FTS Content", score=0.9)])
    searcher._search_mib = AsyncMock(return_value=[SearchResult(page_id=2, title="MIB Doc", content="MIB Content", score=0.8)])

    results = await searcher.search("query", strategy="hybrid")

    # Scorer should have been called for each unique document
    assert mock_scorer.score.call_count >= 2
    assert len(results) == 2
    # Check if they are SearchResult objects
    assert all(isinstance(r, SearchResult) for r in results)


@pytest.mark.asyncio
async def test_search_auto_strategy(mock_cm):
    searcher = RAGSearcher(mock_cm, layer="user")
    searcher._search_fts5 = AsyncMock(return_value=[])
    searcher._search_hybrid = AsyncMock(return_value=[])

    # Short query -> fts
    await searcher.search("short", strategy="auto")
    searcher._search_fts5.assert_called_once()

    # Long query -> hybrid
    await searcher.search("this is a longer query for hybrid", strategy="auto")
    searcher._search_hybrid.assert_called_once()


@pytest.mark.asyncio
async def test_search_hybrid_fallback_to_rrf(mock_cm):
    # No scorer provided
    searcher = RAGSearcher(mock_cm, layer="user", scorer=None)

    # Mock search_rrf to return some dicts
    from unittest.mock import patch

    with patch("rag.searcher.search_rrf", new_callable=AsyncMock) as mock_rrf:
        mock_rrf.return_value = [{"id": 3, "title": "RRF Doc", "content": "RRF Content", "score": 0.5, "source": "rrf"}]

        # We also need to mock _check_fts
        searcher._check_fts = AsyncMock(return_value=True)

        results = await searcher.search("query", strategy="hybrid")

        assert len(results) == 1
        assert results[0].page_id == 3
        mock_rrf.assert_called_once()
