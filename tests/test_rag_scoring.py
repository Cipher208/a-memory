"""Tests for rag/scoring.py — parametrized."""

import pytest

from rag.scoring import CorpusStats, ScoredCandidate, Scorer, ScoringWeights


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, {"relevance": 1.0, "novelty": 0.0}),
        ({"relevance": 2.0, "novelty": 0.5, "type_boost": 0.3}, {"relevance": 2.0, "novelty": 0.5}),
    ],
)
def test_scoring_weights(kwargs, expected):
    w = ScoringWeights(**kwargs)
    for k, v in expected.items():
        assert getattr(w, k) == v


def test_scored_candidate():
    c = ScoredCandidate(id=1, page_id=10, title="T", content="C", wiki_type=None, rrf_score=0.5)
    assert c.id == 1
    assert c.final_score == 0.0
    c2 = ScoredCandidate(id=2, page_id=10, title="T", content="C", wiki_type="error", rrf_score=0.8, bin_score=0.7, hamming=120, degraded=True)
    assert c2.bin_score == 0.7
    assert c2.degraded is True


@pytest.mark.parametrize(
    "total,counts,doc_id,expected",
    [
        (0, {}, 1, 1.0),
        (10, {1: 5}, 999, 0.0),
        (10, {1: 3}, 1, 0.3),
    ],
)
def test_corpus_stats(total, counts, doc_id, expected):
    stats = CorpusStats(total_retrievals=total, doc_retrieval_counts=counts)
    assert stats.prior(doc_id) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize(
    "rrf,bin_score,expected",
    [
        (0.5, None, 0.5),
        (0.6, 0.8, 0.7),
    ],
)
def test_relevance_score(rrf, bin_score, expected):
    scorer = Scorer()
    c = ScoredCandidate(id=1, page_id=1, title="T", content="C", wiki_type=None, rrf_score=rrf, bin_score=bin_score)
    assert abs(scorer._relevance_score(c) - expected) < 1e-9


@pytest.mark.parametrize(
    "counts,total,doc_id,min_novelty",
    [
        ({}, 0, 1, 0.0),
        ({2: 50}, 100, 1, 1.0),
        ({1: 90}, 100, 1, 0.0),
    ],
)
def test_novelty(counts, total, doc_id, min_novelty):
    stats = CorpusStats(total_retrievals=total, doc_retrieval_counts=counts)
    scorer = Scorer(corpus_stats=stats)
    c = ScoredCandidate(id=1, page_id=doc_id, title="T", content="C", wiki_type=None, rrf_score=0.5)
    n = scorer._compute_novelty(c)
    if min_novelty == 0.0:
        assert n < 0.1
    else:
        assert n == pytest.approx(min_novelty, abs=0.01)


def test_novelty_capped():
    stats = CorpusStats(total_retrievals=2, doc_retrieval_counts={2: 1})
    scorer = Scorer(corpus_stats=stats)
    c = ScoredCandidate(id=1, page_id=1, title="T", content="C", wiki_type=None, rrf_score=0.5)
    assert scorer._compute_novelty(c) == 1.0


@pytest.mark.parametrize(
    "wiki_type,expected",
    [
        (None, 0.0),
        ("", 0.0),
        ("error", 0.12),
        ("decision", 0.1),
        ("spec", 0.08),
        ("code", 0.05),
        ("note", 0.02),
        ("random", 0.0),
    ],
)
def test_type_boost(wiki_type, expected):
    assert Scorer()._type_boost(wiki_type) == expected


def test_rank_sync_ordering():
    scorer = Scorer()
    c1 = ScoredCandidate(id=1, page_id=1, title="A", content="C", wiki_type=None, rrf_score=0.3)
    c2 = ScoredCandidate(id=2, page_id=2, title="B", content="C", wiki_type=None, rrf_score=0.7)
    result = scorer.rank_sync("query", [c1, c2], "user1")
    assert result[0].id == 2


def test_rank_sync_novelty():
    stats = CorpusStats(total_retrievals=100, doc_retrieval_counts={1: 90, 2: 5})
    scorer = Scorer(weights=ScoringWeights(relevance=1.0, novelty=1.0), corpus_stats=stats)
    c1 = ScoredCandidate(id=1, page_id=1, title="A", content="C", wiki_type=None, rrf_score=0.5)
    c2 = ScoredCandidate(id=2, page_id=2, title="B", content="C", wiki_type=None, rrf_score=0.5)
    assert scorer.rank_sync("q", [c1, c2], "u")[0].id == 2


def test_rank_sync_type_boost():
    scorer = Scorer(weights=ScoringWeights(relevance=1.0, novelty=0.0, type_boost=1.0))
    c1 = ScoredCandidate(id=1, page_id=1, title="A", content="C", wiki_type="error", rrf_score=0.5)
    c2 = ScoredCandidate(id=2, page_id=2, title="B", content="C", wiki_type="note", rrf_score=0.5)
    assert scorer.rank_sync("q", [c1, c2], "u")[0].id == 1


@pytest.mark.asyncio
async def test_rank_async():
    scorer = Scorer()
    c = ScoredCandidate(id=1, page_id=1, title="T", content="C", wiki_type=None, rrf_score=0.5)
    result = await scorer.rank("query", [c], "user1")
    assert len(result) == 1
    assert result[0].final_score == 0.5
