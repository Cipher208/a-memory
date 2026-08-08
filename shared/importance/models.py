from __future__ import annotations

from pydantic import BaseModel, Field


class ImportanceSignals(BaseModel):
    """Per-signal breakdown before normalization. All in [0,1]."""

    base: float = 0.0
    length: float = 0.0
    question: float = 0.0
    tech_keyword: float = 0.0
    emotional: float = 0.0
    novelty: float = 0.0
    retrieval_signal: float = 0.0
    noise_penalty: float = 0.0


class ImportanceConfig(BaseModel):
    """Configuration for importance scoring."""

    weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)


class ScorerResult(BaseModel):
    """Result of importance scoring."""

    score: float
    signals: ImportanceSignals
