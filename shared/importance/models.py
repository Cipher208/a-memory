from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Dict, Any

class ImportanceSignals(BaseModel):
    base: float = 0.0
    length: float = 0.0
    question: float = 0.0
    tech_keyword: float = 0.0
    emotional: float = 0.0
    novelty: float = 0.0
    retrieval_signal: float = 0.0
    noise_penalty: float = 0.0

class ImportanceConfig(BaseModel):
    weights: Dict[str, float] = Field(default_factory=dict)
    thresholds: Dict[str, float] = Field(default_factory=dict)

class ScorerResult(BaseModel):
    score: float
    signals: ImportanceSignals

    def total(self) -> float:
        return self.score

    @property
    def base(self) -> float: return self.signals.base
    @property
    def length(self) -> float: return self.signals.length
    @property
    def tech_keyword(self) -> float: return self.signals.tech_keyword
    @property
    def novelty(self) -> float: return self.signals.novelty
    @property
    def retrieval_signal(self) -> float: return self.signals.retrieval_signal
    @property
    def emotional(self) -> float: return self.signals.emotional