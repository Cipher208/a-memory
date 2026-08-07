"""Importance v2 — multi-signal scorer.

Replaces naive heuristic in ImportanceGateMiddleware and UserHooks._calculate_importance.
All signals are independent, normalized to [0,1]. Final score in [0,1].
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from shared.memory_types import (
    MemoryKind,
    get_policy,
    kind_for_text,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

# Tech keywords (RU + EN)
_TECH_KEYWORDS_RU = (
    "redis",
    "postgres",
    "postgresql",
    "mysql",
    "sqlite",
    "mongo",
    "docker",
    "kubernetes",
    "k8s",
    "aws",
    "gcp",
    "azure",
    "api",
    "rest",
    "graphql",
    "grpc",
    "kafka",
    "rabbitmq",
    "jwt",
    "oauth",
    "ssl",
    "tls",
    "vpn",
    "ssh",
    "sql",
    "orm",
    "migration",
    "бэкап",
    "резервная копия",
    "ci",
    "cd",
    "деплой",
    "smoke",
    "релиз",
    "продакшн",
    "критично",
    "срочно",
    "блокер",
    "баг",
    "incident",
    "ошибка",
    "exception",
    "traceback",
    "stack trace",
)

_TECH_KEYWORDS_EN = (
    "redis",
    "postgres",
    "postgresql",
    "mysql",
    "sqlite",
    "mongo",
    "docker",
    "kubernetes",
    "k8s",
    "aws",
    "gcp",
    "azure",
    "api",
    "rest",
    "graphql",
    "grpc",
    "kafka",
    "rabbitmq",
    "jwt",
    "oauth",
    "ssl",
    "tls",
    "vpn",
    "ssh",
    "sql",
    "orm",
    "migration",
    "backup",
    "ci",
    "cd",
    "deploy",
    "smoke",
    "release",
    "production",
    "critical",
    "urgent",
    "blocker",
    "bug",
    "incident",
    "error",
    "exception",
    "traceback",
    "stack trace",
)

_NOISE_PATTERNS_RU = (
    r"^ок\.?$",
    r"^ага\.?$",
    r"^угу\.?$",
    r"^да\.?$",
    r"^нет\.?$",
    r"^ладно\.?$",
    r"^хорошо\.?$",
    r"^понял(а)?\.?$",
    r"^принял(а)?\.?$",
    r"^спс\.?$",
    r"^благодарю\.?$",
)
_NOISE_PATTERNS_EN = (
    r"^ok\.?$",
    r"^k\.?$",
    r"^yep\.?$",
    r"^yeah\.?$",
    r"^nope\.?$",
    r"^got it\.?$",
    r"^thanks?\.?$",
    r"^thx\.?$",
    r"^ty\.?$",
)

_EMOTION_HIGH_PRIORITY = {
    MemoryKind.INSTRUCTION,
    MemoryKind.RULE,
    MemoryKind.COMMITMENT,
}


@dataclass
class ImportanceSignals:
    """Per-signal breakdown before normalization. All in [0,1]."""

    base: float = 0.0
    length: float = 0.0
    question: float = 0.0
    tech_keyword: float = 0.0
    emotional: float = 0.0
    novelty: float = 0.0
    retrieval_signal: float = 0.0
    noise_penalty: float = 0.0

    weights: dict[str, float] = field(
        default_factory=lambda: {
            "base": 1.0,
            "length": 0.6,
            "question": 0.5,
            "tech_keyword": 1.0,
            "emotional": 0.8,
            "novelty": 0.7,
            "retrieval_signal": 0.9,
            "noise_penalty": 1.0,
        }
    )

    def total(self) -> float:
        sum_pos = (
            self.base * self.weights["base"]
            + self.length * self.weights["length"]
            + self.question * self.weights["question"]
            + self.tech_keyword * self.weights["tech_keyword"]
            + self.emotional * self.weights["emotional"]
            + self.novelty * self.weights["novelty"]
            + self.retrieval_signal * self.weights["retrieval_signal"]
        )
        max_possible = sum(v for k, v in self.weights.items() if k != "noise_penalty") or 1.0
        raw = sum_pos / max_possible
        # Noise penalty: weight controls how much penalty is applied
        noise_weight = self.weights.get("noise_penalty", 1.0)
        effective_penalty = min(self.noise_penalty, 1.0) * noise_weight
        penalized = raw * (1.0 - min(effective_penalty, 1.0))
        return max(0.0, min(1.0, penalized))


class ImportanceScorer:
    """Multi-signal importance scorer."""

    NOISE_RE_RU = re.compile("|".join(_NOISE_PATTERNS_RU), re.IGNORECASE)
    NOISE_RE_EN = re.compile("|".join(_NOISE_PATTERNS_EN), re.IGNORECASE)

    def __init__(
        self,
        technical_keywords_ru: Iterable[str] = _TECH_KEYWORDS_RU,
        technical_keywords_en: Iterable[str] = _TECH_KEYWORDS_EN,
        weights: dict[str, float] | None = None,
    ):
        self.tech_ru = {k.lower() for k in technical_keywords_ru}
        self.tech_en = {k.lower() for k in technical_keywords_en}
        self._weights = weights or {}

    def score(
        self,
        text: str,
        kind: str | MemoryKind | None = None,
        retrieval_count: int = 0,
        seen_before: bool = False,
        emotion_weight: float = 0.0,
        is_technical_context: bool = False,
    ) -> ImportanceSignals:
        signals = ImportanceSignals()
        if self._weights:
            signals.weights.update(self._weights)

        if not text:
            return signals

        # 1) base from type-policy
        if kind is None:
            kind = kind_for_text(text)
        elif isinstance(kind, str):
            try:
                kind = MemoryKind(kind)
            except ValueError:
                kind = MemoryKind.FACT
        policy = get_policy(kind)
        signals.base = policy.default_importance

        # 2) length bonus — S-curve
        L = len(text)
        signals.length = min(1.0, L / 800.0)

        # 3) question bonus
        qcount = text.count("?")
        signals.question = min(1.0, qcount * 0.5)

        # 4) tech keywords
        tokens = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9_-]+", text.lower())
        hits_ru = sum(1 for t in tokens if t in self.tech_ru)
        hits_en = sum(1 for t in tokens if t in self.tech_en)
        total_hits = hits_ru + hits_en
        signals.tech_keyword = min(1.0, total_hits * 0.25)
        if is_technical_context:
            signals.tech_keyword = min(1.0, signals.tech_keyword + 0.3)

        # 5) emotional
        if kind in _EMOTION_HIGH_PRIORITY:
            signals.emotional = 0.8
        else:
            signals.emotional = max(0.0, min(1.0, emotion_weight))

        # 6) novelty — penalize duplicates
        signals.novelty = 0.0 if seen_before else 0.7

        # 7) retrieval signal — log scale
        if retrieval_count > 0:
            signals.retrieval_signal = min(1.0, math.log1p(retrieval_count) / 5.0)
        else:
            signals.retrieval_signal = 0.2

        # 8) noise penalty
        if self.NOISE_RE_RU.match(text.strip()) or self.NOISE_RE_EN.match(text.strip()):
            signals.noise_penalty = 0.95

        return signals
