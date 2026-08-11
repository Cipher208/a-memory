from __future__ import annotations
import re
from typing import Any, Pattern
from .models import EmotionMarkerConfig, EmotionResult


class EmotionEngine:
    """
    Engine for detecting emotions in text using optimized regex matching.
    """

    PHRASE_SCORE_DEFAULT = 0.5
    MARKER_SCORE_DEFAULT = 0.4
    EMOJI_SCORE_DEFAULT = 0.3

    def __init__(self, config: EmotionMarkerConfig) -> None:
        self.config = config
        self.phrase_regex: Pattern[str] | None = None
        self.marker_regex: Pattern[str] | None = None
        self.emoji_regex: Pattern[str] | None = None
        self._compile()

    def _compile(self) -> None:
        """Compile regex patterns once."""
        phrase_patterns: list[str] = []
        for i, p in enumerate(self.config.phrases):
            pattern = p.pattern.replace(" ", r"\s+(?:\w+\s+)?")
            phrase_patterns.append(f"(?P<p{i}>{pattern})")
        self.phrase_regex = self._build_regex(phrase_patterns, flags=re.IGNORECASE)
        self.marker_regex = self._build_regex(
            [f"(?P<m_{cat.replace(' ', '_')}>{'|'.join(re.escape(w) for w in words)})" for cat, words in self.config.markers.items() if words],
            flags=re.IGNORECASE,
        )
        self.emoji_regex = self._build_regex(
            [f"(?P<e_{cat.replace(' ', '_')}>{'|'.join(re.escape(i) for i in icons)})" for cat, icons in self.config.emojis.items() if icons]
        )

    def _build_regex(self, parts: list[str], flags: int = 0) -> Pattern[str] | None:
        return re.compile("|".join(parts), flags) if parts else None

    def detect(self, text: str) -> list[EmotionResult]:
        """Detects emotions in the given text with Phrase > Marker > Emoji priority."""
        if not text:
            return []

        results: dict[str, EmotionResult] = {}

        self._match_phrases(text, results)
        self._match_markers(text, results)
        self._match_emojis(text, results)

        return list(results.values())

    def _add_result(
        self,
        results: dict[str, EmotionResult],
        emotion: str,
        score: float,
        source: str,
        match_text: str,
    ) -> None:
        emotion = emotion.replace("_", " ")
        if emotion not in results or results[emotion].score < score:
            results[emotion] = EmotionResult(trigger_type=emotion, score=score, metadata={"source": source, "match": match_text})

    def _match_phrases(self, text: str, results: dict[str, EmotionResult]) -> None:
        if not self.phrase_regex:
            return
        for match in self.phrase_regex.finditer(text):
            group_name = match.lastgroup
            if group_name and group_name.startswith("p"):
                phrase = self.config.phrases[int(group_name[1:])]
                self._add_result(results, phrase.emotion, phrase.score, "phrase", match.group())

    def _match_markers(self, text: str, results: dict[str, EmotionResult]) -> None:
        if not self.marker_regex:
            return
        for match in self.marker_regex.finditer(text):
            group_name = match.lastgroup
            if group_name and group_name.startswith("m_"):
                self._add_result(results, group_name[2:], self.MARKER_SCORE_DEFAULT, "marker", match.group())

    def _match_emojis(self, text: str, results: dict[str, EmotionResult]) -> None:
        if not self.emoji_regex:
            return
        for match in self.emoji_regex.finditer(text):
            group_name = match.lastgroup
            if group_name and group_name.startswith("e_"):
                self._add_result(results, group_name[2:], self.EMOJI_SCORE_DEFAULT, "emoji", match.group())
