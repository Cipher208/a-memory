"""S19 tail: RU morphology — lemmatization via pymorphy3 (lazy + graceful).

The lemma collapses inflection ("zarplaty" → "zarplata", "deploili" →
"deployit") for the topic_overlap miner and canon keys. PyWordForm ambiguity
is resolved by the first (most probable) pymorphy parse. Library
absent → normal_form = None → the calling code degrades to raw tokens
(the determinism contract on the hot path is preserved).
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_morph: object | None = None
_tried = False


def _analyzer() -> object | None:
    global _morph, _tried
    if not _tried:
        _tried = True
        try:
            from pymorphy3 import MorphAnalyzer  # type: ignore[import-untyped]

            _morph = MorphAnalyzer()
        except Exception as exc:  # pymorphy3 not installed — normal degradation
            logger.debug("pymorphy3 unavailable — RU lemmas disabled: %s", exc)
            _morph = None
    return _morph


@lru_cache(maxsize=20000)
def normal_form(word: str) -> str | None:
    """Return the lemma of a word; None if pymorphy3 is unavailable (fallback is the caller's)."""
    morph = _analyzer()
    if morph is None:
        return None
    try:
        parsed = morph.parse(word)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("pymorphy parse failed for %r: %s", word, exc)
        return None
    if not parsed:
        return word
    best = parsed[0]
    if best.score < 0.3:
        return None  # word is out of the dictionary (loanwords) — a raw token is more reliable
    lemma = str(best.normal_form)
    # common prefix >= 4 — "deploy"→"deplyy" gets rejected here too; "zarplaty"→"zarplata" passes
    common = sum(1 for a, b in zip(word, lemma, strict=False) if a == b)  # strict zip not needed: pairs run to the end of the shorter one
    if common < min(4, len(lemma)):
        return None
    return lemma


def reset_morph() -> None:
    """Test helper: reset the singleton."""
    global _morph, _tried
    _morph = None
    _tried = False
    normal_form.cache_clear()
