"""Importance Scorer Orchestrator."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

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
    """
    Orchestrator for importance scoring.
    """

    def __init__(
        self,
        config: Optional[ImportanceConfig] = None,
        signals: Optional[List[IImportanceSignal]] = None,
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
        self._tech_re: Optional[re.Pattern] = None
        self._noise_re: Optional[re.Pattern] = None

    def _load_config(self) -> ImportanceConfig:
        if self._config:
            return self._config
        
        # Absolute path check
        path = self._config_path
        if not path.is_absolute():
            # Assume relative to project root /home/murat/Projects/repos/mcp-ariel-memory
            path = Path("/home/murat/Projects/repos/mcp-ariel-memory") / self._config_path

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return ImportanceConfig(**data)

    def _load_data(self) -> tuple[re.Pattern, re.Pattern]:
        path = self._data_path
        if not path.is_absolute():
            path = Path("/home/murat/Projects/repos/mcp-ariel-memory") / self._data_path

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
            tech_patterns = data.get("tech_keywords_ru", []) + data.get("tech_keywords_en", [])
            tech_re = re.compile("|".join(re.escape(k) for k in tech_patterns), re.IGNORECASE)
            
            noise_patterns = data.get("noise_patterns_ru", []) + data.get("noise_patterns_en", [])
            noise_re = re.compile("|".join(noise_patterns), re.IGNORECASE)
            
            return tech_re, noise_re

    def score(self, text: str, context: Optional[Dict[str, Any]] = None) -> ScorerResult:
        """
        Calculate importance score for the given text.
        """
        if context is None:
            context = {}

        config = self._load_config()
        
        # Inject regex into context if not already present
        if "tech_re" not in context or "noise_re" not in context:
            if not self._tech_re or not self._noise_re:
                self._tech_re, self._noise_re = self._load_data()
            context["tech_re"] = self._tech_re
            context["noise_re"] = self._noise_re

        # Run all signals
        results = {}
        for signal in self._signals:
            # Map signal class to ImportanceSignals field name
            name = signal.__class__.__name__.lower().replace("signal", "")
            if name == "basetype":
                name = "base"
            elif name == "techkeyword":
                name = "tech_keyword"
            elif name == "retrieval":
                name = "retrieval_signal"
            elif name == "noise":
                name = "noise_penalty"
            elif name == "emotion":
                name = "emotional"
            
            results[name] = signal.calculate(text, context)

        signals = ImportanceSignals(**results)
        
        # Calculate total score using weights from config
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
        
        # Noise penalty
        noise_weight = weights.get("noise_penalty", 1.0)
        effective_penalty = min(signals.noise_penalty, 1.0) * noise_weight
        penalized = raw * (1.0 - min(effective_penalty, 1.0))
        
        final_score = max(0.0, min(1.0, penalized))
        
        return ScorerResult(score=final_score, signals=signals)
