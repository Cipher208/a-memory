"""S19-хвосты: RU-морфология — лемматизация через pymorphy3 (lazy + graceful).

Лемма схлопывает словоизменение («зарплаты» → «зарплата», «деплоили» →
«деплоить») для topic_overlap-минера и канон-ключей. PyWordForm-неоднозначность
разрешается первым (наиболее вероятным) разбором pymorphy. Библиотека
отсутствует → normal_form = None → вызывающий код деградирует к сырым токенам
(контракт детерминизма на горячем пути не теряется).
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
        except Exception as exc:  # pymorphy3 не установлен — нормальная деградация
            logger.debug("pymorphy3 unavailable — RU lemmas disabled: %s", exc)
            _morph = None
    return _morph


@lru_cache(maxsize=20000)
def normal_form(word: str) -> str | None:
    """Лемма слова; None если pymorphy3 недоступен (fallback у вызывающего)."""
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
        return None  # слово вне словаря (заимствования) — сырой токен надёжнее
    lemma = str(best.normal_form)
    # общий префикс ≥ 4 — «деплой»→«деплый» отвергнется и здесь; «зарплаты»→«зарплата» пройдёт
    common = sum(1 for a, b in zip(word, lemma, strict=False) if a == b)  # строгий zip не нужен: пары до конца кратчайшего
    if common < min(4, len(lemma)):
        return None
    return lemma


def reset_morph() -> None:
    """Test helper: сброс синглтона."""
    global _morph, _tried
    _morph = None
    _tried = False
    normal_form.cache_clear()
