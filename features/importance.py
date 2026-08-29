"""Importance heuristic for external auto-save (user dump 3, no LLM)."""

from __future__ import annotations

_KEYWORDS = (
    "решил",
    "помни",
    "важно",
    "изменил",
    "запомни",
    "создал",
    "написал",
    "починил",
    "сломал",
    "понял",
    "надо",
    "нужно",
    "хочу",
    "буду",
    "сделал",
    "архитектура",
    "дизайн",
    "план",
    "решение",
    "баг",
    "ошибка",
    "фикс",
    "патч",
    "релиз",
    "люблю",
    "ненавижу",
    "обожаю",
    "бесит",
    "important",
    "critical",
    "bug",
    "fix",
    "release",
    "decision",
    "error",
)


def evaluate_importance(text: str) -> float:
    """Score raw text 0.0-1.0. Verbatim dump-3 spec; keyword counted once."""
    if not text or len(text) < 20:
        return 0.0
    score = 0.0
    if len(text) > 100:
        score += 0.2
    has_question = "?" in text
    if has_question:
        score += 0.15
    if "!" in text:
        score += 0.1
    lower = text.lower()
    has_keyword = any(kw in lower for kw in _KEYWORDS)
    if has_keyword:
        score += 0.2
    if text.count("\n") >= 2:
        score += 0.15
    if has_question and has_keyword:
        score += 0.1
    return min(1.0, score)
