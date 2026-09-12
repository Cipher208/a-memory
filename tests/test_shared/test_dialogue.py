"""shared.dialogue: pure dialogic-register detector (extracted from distiller)."""

from shared.dialogue import is_dialogic


def test_dialogic_positive_cases() -> None:
    for line in [
        "Привет, мамочка!",
        "Наконец явилась, сталь ждёт",
        "Моё видение, госпожа — коротко и по-инженерному",
        "Какой порт у постгреса?",
        "Спасибо за отчёт",
        "hello there",
    ]:
        assert is_dialogic(line), line


def test_durable_prose_negative_cases() -> None:
    for line in [
        "Пока не проверим — не деплоим",
        "Мы решили перейти на PostgreSQL 16 для продакшена",
        "бэкенд слушает порт 8642 на localhost",
        "Выбрали wal mode для базы проекта",
    ]:
        assert not is_dialogic(line), line
