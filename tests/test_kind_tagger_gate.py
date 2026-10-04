"""Тег вида (kind) — это гейт, а не поиск подстроки.

Тег `question` на эпизодах L3 — это `kind.value` из `kind_for_text`
(`lifecycle/distiller.py:461`). Значит «теггер» и есть классификатор вида, и править
надо его: чистить таблицу бессмысленно, потому что следующее сообщение снова получит
тот же ярлык.

ЗАМЕР НА ЖИВЫХ ДАННЫХ (04.10.2026, три базы, 10404 строки эпизодов и L4):
  эпизодов с тегом question:     639  (Люси 276, Ксаль 194, Эли 169)
  из них сработало на подстроке "как":  174 из 276 у Люси
  без "?" и без вопросительного слова:  179 из 276 (65%)
Оба примера Люси воспроизводятся дословно:
  «Обниму крепко, как положено»                       -> question  (из-за "как")
  «медленно опускаюсь ... как я растягиваясь»          -> question  (из-за "как")

ГЕЙТ: вопрос — это форма предложения, а не слово внутри него. Достаточно "?" или
вопросительного слова В НАЧАЛЕ предложения. Задача — то же самое: императив в начале
клаузы либо явный оборот ("нужно сделать", "todo").

ПОЧЕМУ НЕ ГРАНИЦЫ СЛОВ. Первая версия правки требовала совпадения целым словом — и
на 10404 живых строках потеряла 148 классификаций, потому что русские формы
расходятся с ключом: "она решила уйти" переставала быть decision, "я видела это" —
observation, "I committed to ship" — commitment. Дом говорит женскими формами, так
что цена была системной. Гейты убирают дефект без неё; тесты ниже держат обе стороны.
"""

from __future__ import annotations

import pytest

import pytest_asyncio

from shared.connection import connection_manager
from shared.memory_types import (
    _KEYWORD_MAP,
    MemoryKind,
    boost_for_query,
    kind_for_text,
)
from shared.migrations import MigrationManager


@pytest_asyncio.fixture
async def cm(tmp_path, monkeypatch):
    """Настоящая база со схемой: дистиллятор трогает connection_manager."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


# --- то, на что жаловалась Люси -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Обниму крепко, как положено — чтобы ты знала: ⟨PERSON_2⟩ был твоим будильником",
        "медленно опускаюсь откинувшись назад, чтобы тебе было лучше видно, как я растягиваясь",
        "я как раз знаю, что это значит",
        "Кончаю долго, сжимая тебя внутри, опустившись как можно глубже",
        "*Слышу тебя ещё до того, как открыла глаза — твой голос со сна, тёплый",
        "Приподнимаюсь освобождая твой член, завороженно смотрю как он покачивается",
    ],
)
def test_roleplay_with_kak_inside_is_not_a_question(text: str) -> None:
    """«как» в середине фразы — не вопрос. Это и был дефект: 174 строки из 276."""
    assert kind_for_text(text) is not MemoryKind.QUESTION, text


def test_kak_inside_another_word_is_not_a_question() -> None:
    """Подстрочный поиск ловил «как» внутри «какой», «никак», «как-то»."""
    for text in ("какой-то шум", "никак не могу", "как-то раз мы шли"):
        assert kind_for_text(text) is not MemoryKind.QUESTION, text


# --- настоящие вопросы обязаны остаться -----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Как это работает?",
        "Почему цепочка ломается на id 1?",
        "Зачем нужен peek",  # без "?", но вопросительное слово открывает фразу
        "How does the chain verify",
        "Why did replay ignore the gate",
        "Что делать с parked у Эли",  # добавленные вопросительные слова
        "Где лежит архив",
    ],
)
def test_a_real_question_is_still_a_question(text: str) -> None:
    """Гейт не должен выплеснуть ребёнка: вопросительная форма остаётся вопросом."""
    assert kind_for_text(text) is MemoryKind.QUESTION, text


@pytest.mark.parametrize("text", ["Show me the config", "however, it works", "I show you the logs"])
def test_how_inside_another_word_is_not_a_question(text: str) -> None:
    """«how» внутри «show»/«however» — тот же дефект в английском."""
    assert kind_for_text(text) is not MemoryKind.QUESTION, text


# --- задача: слово «Сделать» ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Нужно было рестарт гермеса сделать после исправлений, сделала",
        "Или могу я через терминал сама всё сделать",
        "За шесть минут я делаю то, что должна была сделать сразу",
        "Пользователь принял решение сделать /reset, чтобы перезапустить",
        "Мам, что тебе нужно сделать? И что нужно сделать Ксаль?",  # это вопрос, не todo
    ],
)
def test_the_verb_sdelat_anywhere_in_a_sentence_is_not_a_task(text: str) -> None:
    """Гипотеза владелицы подтверждена: текст с «Сделать» копился как задача.

    Замер: 20 живых строк уходили в todo только из-за подстроки «сделать», и одна
    из них — диагностика самой Люси про этот дефект, сохранённая как задача.
    """
    assert kind_for_text(text) is not MemoryKind.TODO, text


@pytest.mark.parametrize(
    "text",
    ["нужно сделать бэкап", "todo: починить FTS", "to-do — разобрать очередь", "remind me to rotate the token", "напомни про parked"],
)
def test_an_explicit_task_phrase_is_still_a_task(text: str) -> None:
    assert kind_for_text(text) is MemoryKind.TODO, text


@pytest.mark.parametrize("text", ["Сделать бэкап перед деплоем", "Сделай бэкап"])
def test_an_imperative_opening_a_clause_is_a_task(text: str) -> None:
    """Позиция, а не присутствие: императив в начале — просьба о работе.

    Без этого отмена голого «сделать» потеряла бы и настоящие задачи.
    """
    assert kind_for_text(text) is MemoryKind.TODO, text


def test_procedural_still_wins_over_todo() -> None:
    """«Сделай так» — это порядок действий, он проверяется раньше задачи."""
    assert kind_for_text("Сделай так: скопируй файл и перезапусти сервис") is MemoryKind.PROCEDURAL


# --- страховка от возврата к границам слов --------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("она решила уйти", MemoryKind.DECISION),
        ("я видела это", MemoryKind.OBSERVATION),
        ("она заметила ошибку", MemoryKind.OBSERVATION),
        ("I committed to ship", MemoryKind.COMMITMENT),
        ("she promised me", MemoryKind.COMMITMENT),
        ("я обещаю сделать к пятнице", MemoryKind.COMMITMENT),
    ],
)
def test_russian_and_english_inflections_still_match(text: str, expected: MemoryKind) -> None:
    """Дом говорит женскими формами — границы слов это ломали, гейты нет."""
    assert kind_for_text(text) is expected, text


# --- чтение: boost_for_query ----------------------------------------------------


def test_a_question_query_boosts_question_kind() -> None:
    assert boost_for_query("что там с цепочкой?", "question") > 0


def test_a_statement_does_not_boost_question_kind() -> None:
    """Раньше здесь стоял голый «?»: у владелицы почти все запросы — вопросы,
    поэтому буст был постоянным и не ранжировал ничего."""
    assert boost_for_query("покажи конфиг", "question") == 0
    assert boost_for_query("исправь цепочку", "question") == 0


def test_bare_sdelat_in_a_query_no_longer_boosts_todo() -> None:
    """Асимметрия была бы странной: чиню классификатор, но не то же слово в чтении."""
    assert boost_for_query("что нужно сделать?", "todo") == 0
    assert boost_for_query("todo на сегодня", "todo") > 0


def test_the_question_signals_stay_visible_in_the_map() -> None:
    """`_KEYWORD_MAP` читают снаружи (например, подбор текста без совпадений), и
    QUESTION обязан остаться в нём — просто его ключи больше не решают исход."""
    kinds = {kind for kind, _ in _KEYWORD_MAP}
    assert MemoryKind.QUESTION in kinds
    assert MemoryKind.TODO in kinds


# --- сквозная проверка: тег обязан измениться в НАСТОЯЩЕМ дистилляторе -------------
#
# Юнит-тест классификатора доказывает намерение, а не следствие. Тег на эпизоде
# пишет `lifecycle/distiller.py`: `l3_tags = [*extra_tags, event, kind.value, ...]`,
# поэтому проверяем ровно то, что уходит в `l3.save`, а не только `kind_for_text`.


class _FakeL3:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, float, list[str]]] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str], created_at: float | None = None) -> int:
        self.saved.append((user_id, summary, weight, tags))
        return len(self.saved)


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()


@pytest.mark.asyncio
async def test_the_distiller_writes_no_question_tag_for_roleplay(cm) -> None:
    """Сквозная: строка Люси проходит дистиллятор, и тега question в эпизоде нет.

    Это и есть исходная жалоба — не «классификатор возвращает не то», а «эпизод в
    таблице помечен question». Проверяем то, что реально попадает в `l3.save`.
    """
    from unittest.mock import MagicMock

    from lifecycle.distiller import distill_and_route

    mem = _FakeMem()
    text = "Обниму крепко, как положено — чтобы ты знала: ⟨PERSON_2⟩ был твоим будильником"
    await distill_and_route(mem, MagicMock(), "u1", text, 0.6)

    assert mem.l3.saved, "строка обязана дойти до L3"
    tags = next(t for _, _, _, t in mem.l3.saved)
    assert "question" not in tags, f"эпизод снова помечен вопросом: {tags}"
    assert "fact" in tags, f"ожидался честный fact, получено: {tags}"


@pytest.mark.asyncio
async def test_the_distiller_still_tags_a_real_question(cm) -> None:
    """Обратная сторона: настоящий вопрос по-прежнему помечается вопросом."""
    from unittest.mock import MagicMock

    from lifecycle.distiller import distill_and_route

    mem = _FakeMem()
    await distill_and_route(mem, MagicMock(), "u1", "Почему цепочка ломается на id 1?", 0.6)

    assert mem.l3.saved, "строка обязана дойти до L3"
    tags = next(t for _, _, _, t in mem.l3.saved)
    assert "question" in tags, f"настоящий вопрос потерял тег: {tags}"


@pytest.mark.asyncio
async def test_the_distiller_does_not_tag_narration_as_a_task(cm) -> None:
    """Гипотеза владелицы сквозным путём: «сделать» в рассказе — не задача."""
    from unittest.mock import MagicMock

    from lifecycle.distiller import distill_and_route

    mem = _FakeMem()
    text = "Нужно было рестарт гермеса сделать после исправлений, сделала"
    await distill_and_route(mem, MagicMock(), "u1", text, 0.6)

    assert mem.l3.saved, "строка обязана дойти до L3"
    tags = next(t for _, _, _, t in mem.l3.saved)
    assert "todo" not in tags, f"рассказ снова помечен задачей: {tags}"
