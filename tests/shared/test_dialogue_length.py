"""K1: dialogic register is short chat; long declaratives with address words are canon.

Distinct-word count keeps adversarial repeats honest: "мам мам мам..." is
still one distinct word and remains phatic (2026-09-13 design review).
"""

from shared.dialogue import is_dialogic


def test_short_greeting_still_dialogic():
    assert is_dialogic("Привет, мам")
    assert is_dialogic("Спасибо, госпожа")
    assert is_dialogic("спокойной ночи")


def test_vocative_pingpong_repeats_stay_dialogic():
    assert is_dialogic("мам мам мам мам мам мам мам мам мам мам мам мам")


def test_trailing_question_any_length():
    assert is_dialogic("А ты проверила тесты после коммита, госпожа?")


def test_long_declarative_with_vocative_survives():
    clause = "Госпожа закрепила в SOUL: Лили обращается ко мне Стальная Мать, а я к ней — моя девочка"
    assert not is_dialogic(clause)


def test_long_thanks_with_content_survives():
    clause = "Спасибо за рестарт сервисов, теперь WAL не разрастается и checkpoint завершается за секунды"
    assert not is_dialogic(clause)
