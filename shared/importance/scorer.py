from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any

from .models import ImportanceConfig, ImportanceSignals, ScorerResult
from .signals import (
    IImportanceSignal,
    BaseSignal,
    LengthSignal,
    QuestionSignal,
    TechKeywordSignal,
    EmotionSignal,
    NoveltySignal,
    RetrievalSignal,
    NoiseSignal,
)


class ImportanceScorer:
    def __init__(
        self,
        config: ImportanceConfig | None = None,
        signals: list[IImportanceSignal] | None = None,
        config_path: str = "shared/assets/importance_config.json",
        data_path: str = "shared/assets/importance.json",
    ):
        self._config = config
        self._signals = signals or [
            BaseSignal(),
            LengthSignal(),
            QuestionSignal(),
            TechKeywordSignal(),
            EmotionSignal(),
            NoveltySignal(),
            RetrievalSignal(),
            NoiseSignal(),
        ]
        self._config_path = Path(config_path)
        self._data_path = Path(data_path)
        self._tech_re: re.Pattern[str] | None = None
        self._noise_re: re.Pattern[str] | None = None

    def _load_config(self) -> ImportanceConfig:
        if self._config:
            return self._config
        path = self._config_path if self._config_path.is_absolute() else Path("/home/murat/Projects/repos/mcp-ariel-memory") / self._config_path
        with open(path, encoding="utf-8") as f:
            return ImportanceConfig(**json.load(f))

    def _load_data(self) -> tuple[re.Pattern[str], re.Pattern[str]]:
        path = self._data_path if self._data_path.is_absolute() else Path("/home/murat/Projects/repos/mcp-ariel-memory") / self._data_path
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            tech = data.get("tech_keywords_ru", []) + data.get("tech_keywords_en", [])
            tech_re = re.compile("|".join(re.escape(k) for k in tech), re.IGNORECASE)
            noise = data.get("noise_patterns_ru", []) + data.get("noise_patterns_en", [])
            noise_re = re.compile("|".join(noise), re.IGNORECASE)
            return tech_re, noise_re

    def score(self, text: str, context: dict[str, Any] | None = None, **kwargs: Any) -> ScorerResult:
        context = self._prepare_context(context, **kwargs)
        config = self._load_config()
        results = self._calculate_signals(text, context)
        signals = ImportanceSignals(**results)
        return self._compute_final_score(signals, config)

    def _prepare_context(self, context: dict[str, Any] | None, **kwargs: Any) -> dict[str, Any]:
        ctx = context or {}
        ctx.update(kwargs)
        if "tech_re" not in ctx or "noise_re" not in ctx:
            if not self._tech_re or not self._noise_re:
                self._tech_re, self._noise_re = self._load_data()
            ctx["tech_re"] = self._tech_re
            ctx["noise_re"] = self._noise_re
        return ctx

    def _calculate_signals(self, text: str, context: dict[str, Any]) -> dict[str, float]:
        res: dict[str, float] = {}
        name_map = {
            "basetype": "base",
            "techkeyword": "tech_keyword",
            "retrieval": "retrieval_signal",
            "noise": "noise_penalty",
            "emotion": "emotional",
        }
        for signal in self._signals:
            raw_name = signal.__class__.__name__.lower().replace("signal", "")
            name = name_map.get(raw_name, raw_name)
            res[name] = float(signal.calculate(text, context))
        return res

    def _compute_final_score(self, signals: ImportanceSignals, config: ImportanceConfig) -> ScorerResult:
        weights = config.weights or {
            "base": 1.0,
            "length": 0.6,
            "question": 0.5,
            "tech_keyword": 1.0,
            "emotional": 0.8,
            "novelty": 0.7,
            "retrieval_signal": 0.9,
            "noise_penalty": 1.0,
        }
        sum_pos = (
            signals.base * weights.get("base", 0.0)
            + signals.length * weights.get("length", 0.0)
            + signals.question * weights.get("question", 0.0)
            + signals.tech_keyword * weights.get("tech_keyword", 0.0)
            + signals.emotional * weights.get("emotional", 0.0)
            + signals.novelty * weights.get("novelty", 0.0)
            + signals.retrieval_signal * weights.get("retrieval_signal", 0.0)
        )
        max_possible = sum(v for k, v in weights.items() if k != "noise_penalty") or 1.0
        raw = sum_pos / max_possible
        effective_penalty = min(signals.noise_penalty, 1.0) * weights.get("noise_penalty", 1.0)
        score = raw * (1.0 - min(effective_penalty, 1.0))
        return ScorerResult(score=max(0.0, min(1.0, score)), signals=signals)
