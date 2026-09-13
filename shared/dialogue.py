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


def is_dialogic(clause: str, max_words: int = 8) -> bool:
    """Return True for conversational-register clauses — chat, never a durable fact.

    Trailing interrogative is length-independent. Word/phrase register matches
    only condemn SHORT clauses (< max_words distinct words): the owners embed
    durable instructions and persona canon inside address terms ("Госпожа
    закрепила в SOUL: ..."), and the 2026-09-13 gate audit showed the
    length-blind word kill made every such clause unpromotable (spec S3).
    Distinct-word counting keeps vocative ping-pong ("мам" x12) phatic.
    """
    low = clause.lower()
    if low.rstrip().rstrip("!.…;:»\"'").endswith("?"):
        return True
    words = set(re.findall(r"[а-яёa-z]+", low))
    if len(words) > max_words:
        return False
    if words & DIALOGIC_WORDS:
        return True
    return any(p in low for p in DIALOGIC_PHRASES)
