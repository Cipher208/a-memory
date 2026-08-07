"""
Emotion Trigger — detecting important moments by emotional markers.
Supports Russian + English. Rule-based + pattern detection.
"""

import re

EMOTION_MARKERS = {
    "love": [
        "люблю",
        "обожаю",
        "адорю",
        "влюблён",
        "дорогой",
        "родной",
        "love",
        "adore",
        "beloved",
        "dear",
    ],
    "fear": [
        "боюсь",
        "страшно",
        "ужас",
        "паник",
        "тревог",
        "опасаюсь",
        "afraid",
        "scared",
        "terrified",
        "panic",
        "anxiety",
    ],
    "anger": [
        "ненавижу",
        "бесит",
        "злюсь",
        "раздраж",
        "гнев",
        "отвращ",
        "hate",
        "angry",
        "furious",
        "frustrated",
        "annoyed",
    ],
    "joy": [
        "счастлив",
        "рад",
        "весел",
        "отличн",
        "прекрасн",
        "восхитительн",
        "ура",
        "класс",
        "супер",
        "круто",
        "happy",
        "glad",
        "cheerful",
        "wonderful",
        "amazing",
        "awesome",
    ],
    "gratitude": [
        "спасибо",
        "благодарю",
        "благодарн",
        "признателен",
        "thanks",
        "thank you",
        "grateful",
        "appreciate",
    ],
    "importance": [
        "важно",
        "критично",
        "срочно",
        "необходимо",
        "обязательно",
        "запомни",
        "никогда не забудь",
        "запиши",
        "important",
        "critical",
        "urgent",
        "remember",
        "never forget",
    ],
    "sadness": [
        "грустно",
        "печальн",
        "тоск",
        "одинок",
        "жаль",
        "жалею",
        "грусть",
        "слёзы",
        "плач",
        "sad",
        "sorrow",
        "lonely",
        "regret",
        "tears",
    ],
    "surprise": [
        "удивлён",
        "неожиданн",
        "внезапно",
        "нечаянно",
        "surprised",
        "unexpected",
        "suddenly",
        "wow",
    ],
}

PHRASE_PATTERNS = [
    (r"я тебя (люблю|обожаю)", "love", 0.8),
    (r"мне (нравится|нравишься)", "love", 0.6),
    (r"(боюсь|страшно) что", "fear", 0.6),
    (r"я (злюсь|раздражён)", "anger", 0.6),
    (r"(спасибо|благодарю) за", "gratitude", 0.5),
    (r"(важно|критично) чтобы", "importance", 0.7),
    (r"никогда не (забудь|забывай)", "importance", 0.8),
    (r"(хорошо|отлично|прекрасно) что", "joy", 0.5),
    (r"мне (грустно|печально)", "sadness", 0.6),
    (r"как (неожиданно|удивительно)", "surprise", 0.5),
]

PHRASE_PATTERNS_EN = [
    (r"i (love|adore) you", "love", 0.8),
    (r"i (really |so )?(like|enjoy)", "love", 0.6),
    (r"i('m| am) (afraid|scared) that", "fear", 0.6),
    (r"i (hate|despise)", "anger", 0.6),
    (r"(thank you|thanks) (so much|a lot|for)", "gratitude", 0.5),
    (r"(important|critical) that", "importance", 0.7),
    (r"never (forget|forget this)", "importance", 0.8),
    (r"(great|wonderful|amazing) that", "joy", 0.5),
    (r"i('m| am) (sad|upset)", "sadness", 0.6),
    (r"(wow|oh my god|no way)", "surprise", 0.5),
]

EMOJI_MARKERS = {
    "love": ["❤️", "😍", "🥰", "💕", "❤"],
    "fear": ["😨", "😱", "😰", "😥"],
    "anger": ["😡", "🤬", "😤", "💢"],
    "joy": ["😊", "🎉", "😄", "🥳", "😁"],
    "sadness": ["😢", "😭", "😞", "😔"],
    "surprise": ["😲", "🤯", "😮", "❗"],
}

STATE_SHIFT_THRESHOLD = 0.15


class EmotionTrigger:
    def should_save(self, message: str, emotional_state: dict | None = None, state_delta: dict | None = None) -> tuple[bool, str, float]:
        msg_lower = message.lower()

        result = self._check_phrase_patterns(msg_lower)
        if result:
            return result

        result = self._check_emotion_markers(msg_lower)
        if result:
            return result

        result = self._check_emoji(message)
        if result:
            return result

        result = self._check_emotional_state(emotional_state)
        if result:
            return result

        result = self._check_state_shift(state_delta)
        if result:
            return result

        if len(message) > 300:
            return True, "long_message", 0.3

        if message.count("?") >= 3:
            return True, "complex_question", 0.4

        if message.count("!") >= 2:
            return True, "exclamation", 0.3

        return False, "", 0.0

    def _check_phrase_patterns(self, msg_lower: str) -> tuple[bool, str, float] | None:
        for pattern, emotion, weight in PHRASE_PATTERNS + PHRASE_PATTERNS_EN:
            if re.search(pattern, msg_lower):
                return True, f"emotion_{emotion}", weight
        return None

    def _check_emotion_markers(self, msg_lower: str) -> tuple[bool, str, float] | None:
        high_weight = ("love", "fear", "anger")
        for emotion, markers in EMOTION_MARKERS.items():
            for marker in markers:
                if marker in msg_lower:
                    weight = 0.7 if emotion in high_weight else 0.5
                    return True, f"emotion_{emotion}", weight
        return None

    def _check_emoji(self, message: str) -> tuple[bool, str, float] | None:
        high_weight = ("love", "fear", "anger")
        for emotion, emojis in EMOJI_MARKERS.items():
            for emoji in emojis:
                if emoji in message:
                    weight = 0.7 if emotion in high_weight else 0.5
                    return True, f"emotion_{emotion}", weight
        return None

    def _check_emotional_state(self, emotional_state: dict | None) -> tuple[bool, str, float] | None:
        if emotional_state and (emotional_state.get("joy", 0) > 0.8 or emotional_state.get("interest", 0) > 0.8):
            return True, "high_emotion", 0.6
        return None

    def _check_state_shift(self, state_delta: dict | None) -> tuple[bool, str, float] | None:
        if state_delta:
            for key, delta in state_delta.items():
                if abs(delta) > STATE_SHIFT_THRESHOLD:
                    return True, f"state_shift_{key}", 0.4
        return None
