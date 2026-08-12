"""Unified scoring for RAG search.

Replaces parallel ITS+RRF implementations with a single Scorer
that blends relevance, novelty, and type-boost weights.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScoringWeights:
    """Weights for blending scoring dimensions."""

    relevance: float = 1.0
    novelty: float = 0.0
    type_boost: float = 0.0


@dataclass
class ScoredCandidate:
    """A candidate result with scoring metadata."""

    id: int
    page_id: int
    title: str
    content: str
    wiki_type: str | None
    rrf_score: float
    bin_score: float | None = None
    hamming: int | None = None
    source: str = ""
    memory_kind: str | None = None
    degraded: bool = False
    novelty: float = 0.0
    type_boost: float = 0.0
    final_score: float = 0.0
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass
class CorpusStats:
    """Statistics for novelty calculation based on retrieval history."""

    total_retrievals: int = 0
    doc_retrieval_counts: dict[int, int] = field(default_factory=dict)

    def prior(self, doc_id: int) -> float:
        """Prior probability that a document has been retrieved before.

        Returns 1.0 for new documents (full novelty), decreasing as
        the document appears in more retrievals.
        """
        if self.total_retrievals == 0:
            return 1.0
        return self.doc_retrieval_counts.get(doc_id, 0) / self.total_retrievals


class Scorer:
    """Unified scorer for RAG candidates.

    Blends relevance (RRF + optional binary score) with novelty and
    type-boost weights into a single final_score.
    """

    def __init__(
        self,
        mode: str = "rrf",
        weights: ScoringWeights | None = None,
        corpus_stats: CorpusStats | None = None,
    ):
        self.mode = mode
        self.weights = weights or ScoringWeights()
        self.corpus_stats = corpus_stats

    def _relevance_score(self, c: ScoredCandidate) -> float:
        """Compute relevance from rrf_score and optional bin_score."""
        score = c.rrf_score
        if c.bin_score is not None:
            score = (score + c.bin_score) / 2.0
        return score

    def _compute_novelty(self, candidate: ScoredCandidate) -> float:
        """ITS-inspired novelty: surprise = -log2(prior) / log2(N).

        Lower prior → higher surprise → more "novel".
        """
        if self.corpus_stats is None:
            return 0.0
        prior = self.corpus_stats.prior(candidate.page_id)
        surprise = -math.log2(max(prior, 1e-6)) / math.log2(max(self.corpus_stats.total_retrievals, 2))
        return min(surprise, 1.0)

    def _type_boost(self, wiki_type: str | None) -> float:
        """Calculate simple type boost based on wiki_type."""
        if not wiki_type:
            return 0.0
        boosts = {
            "decision": 0.1,
            "spec": 0.08,
            "error": 0.12,
            "code": 0.05,
            "note": 0.02,
        }
        return boosts.get(wiki_type, 0.0)

    def rank_sync(
        self,
        query: str,
        candidates: list[ScoredCandidate],
        user_id: str,
    ) -> list[ScoredCandidate]:
        """Rank candidates synchronously.

        Blends relevance, novelty, and type_boost into final_score.
        Returns sorted list (descending final_score).
        """
        w = self.weights

        for c in candidates:
            rel = self._relevance_score(c)
            c.novelty = self._compute_novelty(c)
            tb = self._type_boost(c.wiki_type)

            c.type_boost = tb
            c.final_score = w.relevance * rel + w.novelty * c.novelty + w.type_boost * tb
            c.debug = {
                "relevance": rel,
                "novelty": c.novelty,
                "type_boost": tb,
                "weights": {
                    "relevance": w.relevance,
                    "novelty": w.novelty,
                    "type_boost": w.type_boost,
                },
            }

        return sorted(candidates, key=lambda x: -x.final_score)

    async def rank(
        self,
        query: str,
        candidates: list[ScoredCandidate],
        user_id: str,
    ) -> list[ScoredCandidate]:
        """Async wrapper around rank_sync."""
        return self.rank_sync(query, candidates, user_id)

    def update_weights(self, feedback: dict[str, float]) -> None:
        """Update scoring weights from user feedback (bandit-style).

        Args:
            feedback: dict with keys 'relevance', 'novelty', 'type_boost'
                     and float values (higher = more important).

        """
        if "relevance" in feedback:
            self.weights = ScoringWeights(
                relevance=max(0.0, min(2.0, feedback.get("relevance", self.weights.relevance))),
                novelty=max(0.0, min(2.0, feedback.get("novelty", self.weights.novelty))),
                type_boost=max(0.0, min(2.0, feedback.get("type_boost", self.weights.type_boost))),
            )
