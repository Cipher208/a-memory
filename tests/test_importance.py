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


# ── dialogic penalty (F1 follow-up, 2026-09-12) ──


def test_dialogic_short_chat_capped_below_gate() -> None:
    # vocative chat with structural bonuses (exclamation, keywords, newlines)
    text = "Спасибо, мамочка! Важно! Нужно решить.\nПривет, дорогая! Важно! Спасибо!\nЕщё раз, важно!"
    assert evaluate_importance(text) <= 0.35


def test_address_embedded_in_long_content_not_capped() -> None:
    # The F1 false-positive class: operational instruction wrapped in address.
    # The scorer's unit is the whole message; clause-level dialogic dies in
    # the distiller guard, not here.
    text = (
        "Умница, мам. Обнови Headroom: cache-read есть и у polza, и у plusvibe, "
        "на них cowagent и hermes сейчас, mimocode идёт не через headroom, надо разобраться.\n"
        "Дашборд тоже проверь заодно, там были вопросы по портам и провайдерам."
    )
    assert evaluate_importance(text) >= 0.4


def test_durable_high_score_not_penalized() -> None:
    text = (
        "Важно: решили изменить архитектуру хранения.\n"
        "Был баг в провайдере кэша, сделали фикс перед релизом.\n"
        "Поняли причину ошибки, нужен патч и план миграции конфигов на проде."
    )
    score = evaluate_importance(text)
    assert score >= 0.5, f"legit technical text capped: {score}"


def test_broadcast_capped_below_gate() -> None:
    """Status broadcast (fixture line): structural reward, penalty must remove it."""
    from features.importance import structure_score

    text = (
        "STAGE 2 ПЛАН B SHIPPED (2026-09-07, push 3a41477..040fb34: 3a41477 Plan A "
        "URIs уже был + annotations коммиты, gate 1499/0 + mypy 232 clean). "
        "mcp_server/annotations получил read-only surface."
    )
    assert structure_score(text) >= 0.4
    assert evaluate_importance(text) < 0.4
