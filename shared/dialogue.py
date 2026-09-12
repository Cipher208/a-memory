"""Dialogic-register detector: greetings, vocatives, questions — chat, not memory.

Pure module (stdlib only): the importance scorer, the distiller and the
consolidation engine all consume it without cycles. Extracted 2026-09-12
after the F1 audit showed episode_promotion keying the owner's greeting as
an L4 'fact'. Word-boundary matching keeps homograph adjectives/conjunctions
(poka=until, khoroshaya=good, dorogaya=dear) from false-positive hits.
"""

from __future__ import annotations

import re

DIALOGIC_WORDS = frozenset(
    {
        "госпожа",
        "господин",
        "мамочка",
        "мама",
        "мам",
        "детка",
        "привет",
        "здравствуй",
        "здравствуйте",
        "приветствую",
        "благодарю",
        "спасибо",
        "прощай",
        "явилась",
        "hello",
        "hi",
        "hey",
        "thanks",
        "thank",
        "darling",
    }
)
DIALOGIC_PHRASES = (
    "доброе утро",
    "добрый день",
    "добрый вечер",
    "спокойной ночи",
    "моя хорошая",
    "моя милая",
    "good morning",
    "good evening",
    "good night",
    "thank you",
)


def is_dialogic(clause: str) -> bool:
    """Return True for conversational-register clauses — chat, never a durable fact.

    Trailing interrogative only (punctuation tails tolerated); a leading "?!"
    is a scoring artifact in synthetic texts, not a question.
    """
    low = clause.lower()
    if low.rstrip().rstrip("!.…;:»\"'").endswith("?"):
        return True
    if set(re.findall(r"[а-яёa-z]+", low)) & DIALOGIC_WORDS:
        return True
    return any(p in low for p in DIALOGIC_PHRASES)
