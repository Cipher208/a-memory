from __future__ import annotations
import re
from .models import EmotionMarkerConfig, EmotionResult


class EmotionEngine:
    """
    Engine for detecting emotions in text using optimized regex matching.
    """

    def __init__(self, config: EmotionMarkerConfig):
        self.config = config
        self.phrase_regex: re.Pattern | None = None
        self.marker_regex: re.Pattern | None = None
        self.emoji_regex: re.Pattern | None = None
        self._compile()

    def _compile(self):
        # 1. Phrases - Using named groups p0, p1, ...
        # Optimization: allow optional words between main tokens in patterns
        phrase_parts = []
        for i, p in enumerate(self.config.phrases):
            # Transform spaces into a flexible pattern that allows for optional words
            # "я тебя люблю" -> "я\s+(?:\w+\s+)?тебя\s+(?:\w+\s+)?люблю"
            pattern = p.pattern.replace(" ", r"\s+(?:\w+\s+)?")
            phrase_parts.append(f"(?P<p{i}>{pattern})")

        if phrase_parts:
            self.phrase_regex = re.compile("|".join(phrase_parts), re.IGNORECASE)

        # 2. Markers - Using named groups m_category
        marker_parts = []
        for category, words in self.config.markers.items():
            if not words:
                continue
            # Escape words and join. Note: \b is not reliable for RU in 're' without extra flags,
            # but we follow the optimization goal of one large regex.
            escaped_words = "|".join(re.escape(w) for w in words)
            marker_parts.append(f"(?P<m_{category.replace(' ', '_')}>{escaped_words})")

        if marker_parts:
            self.marker_regex = re.compile("|".join(marker_parts), re.IGNORECASE)

        # 3. Emojis - Using named groups e_category
        emoji_parts = []
        for category, icons in self.config.emojis.items():
            if not icons:
                continue
            escaped_icons = "|".join(re.escape(i) for i in icons)
            emoji_parts.append(f"(?P<e_{category.replace(' ', '_')}>{escaped_icons})")

        if emoji_parts:
            self.emoji_regex = re.compile("|".join(emoji_parts))

    def detect(self, text: str) -> list[EmotionResult]:
        """
        Detects emotions in the given text.
        Priority: Phrases > Markers > Emojis.
        Returns deduplicated results with highest scores per emotion.
        """
        if not text:
            return []

        results: dict[str, EmotionResult] = {}

        def add_result(emotion: str, score: float, source: str, match_text: str):
            # Clean category name (reverse replace)
            emotion = emotion.replace("_", " ")
            if emotion not in results or results[emotion].score < score:
                results[emotion] = EmotionResult(trigger_type=emotion, score=score, metadata={"source": source, "match": match_text})

        # 1. Phrases (Highest priority/explicit scores)
        if self.phrase_regex:
            for match in self.phrase_regex.finditer(text):
                group_name = match.lastgroup
                if group_name and group_name.startswith("p"):
                    idx = int(group_name[1:])
                    phrase_def = self.config.phrases[idx]
                    add_result(phrase_def.emotion, phrase_def.score, "phrase", match.group())

        # 2. Word Markers (Default score 0.4)
        if self.marker_regex:
            for match in self.marker_regex.finditer(text):
                group_name = match.lastgroup
                if group_name and group_name.startswith("m_"):
                    category = group_name[2:]
                    add_result(category, 0.4, "marker", match.group())

        # 3. Emojis (Default score 0.3)
        if self.emoji_regex:
            for match in self.emoji_regex.finditer(text):
                group_name = match.lastgroup
                if group_name and group_name.startswith("e_"):
                    category = group_name[2:]
                    add_result(category, 0.3, "emoji", match.group())

        return list(results.values())
