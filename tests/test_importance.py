"""evaluate_importance truth table (spec S4, user dump 3 heuristics)."""

import pytest

from features.importance import evaluate_importance


def test_noise_short_text_is_zero() -> None:
    assert evaluate_importance("ok") == 0.0


def test_long_text_bonus() -> None:
    assert evaluate_importance("a" * 120) == pytest.approx(0.2)


def test_question_and_exclamation() -> None:
    assert evaluate_importance("что делать " + "x" * 30 + "?") == pytest.approx(0.15)
    assert evaluate_importance("готово! " + "x" * 30) == pytest.approx(0.1)


def test_keyword_counts_once() -> None:
    text = "решил bug и еще раз решил " + "x" * 100  # len 126 > 100
    assert evaluate_importance(text) == pytest.approx(0.4)  # keyword 0.2 + len>100 0.2


def test_multiline_bonus() -> None:
    assert evaluate_importance("line one\nline two\nline three " + "x" * 80) == pytest.approx(0.35)  # len 0.2 + \n 0.15


def test_question_plus_keyword_bonus() -> None:
    text = "какое решение приняли? " + "x" * 60
    # ? 0.15 + keyword 0.2 + ?+keyword 0.1
    assert evaluate_importance(text) == pytest.approx(0.45)


def test_all_rules_sum_and_cap_guard() -> None:
    text = "?!" + "решил " * 2 + "x" * 120 + "\n" * 3
    # max achievable: 0.2 len + 0.15 ? + 0.1 ! + 0.2 kw + 0.15 \n + 0.1 ?+kw = 0.9
    assert evaluate_importance(text) == pytest.approx(0.9)
    assert evaluate_importance(text) <= 1.0  # cap guard stays


def test_russian_and_english_keywords() -> None:
    for kw in ("важно", "релиз", "важно".upper(), "bug", "fix"):
        assert evaluate_importance(f"{kw} " + "x" * 30) >= 0.2
