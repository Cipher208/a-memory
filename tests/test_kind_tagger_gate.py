"""Тег вида (kind) — это гейт, а не поиск подстроки.

Тег `question` на эпизодах L3 — это `kind.value` из `kind_for_text`
(`lifecycle/distiller.py:461`). Значит «теггер» и есть классификатор вида, и править
надо его: чистить таблицу бессмысленно, потому что следующее сообщение снова получит
тот же ярлык.

ЗАМЕР НА ЖИВЫХ ДАННЫХ (04.10.2026, три базы, 10404 строки эпизодов и L4):
  эпизодов с тегом question:     639  (по базам: 276, 194, 169)
  из них сработало на подстроке "как":  174 из 276 в самой крупной базе
  без "?" и без вопросительного слова:  179 из 276 (65%)
Оба примера воспроизводятся дословно:
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
    get_policy,
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


# --- то, на что жаловались в проде -------------------------------------------------


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
        "Что делать с parked в базе",  # добавленные вопросительные слова
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
        "Мам, что тебе нужно сделать? И что нужно сделать коллеге?",  # это вопрос, не todo
    ],
)
def test_the_verb_sdelat_anywhere_in_a_sentence_is_not_a_task(text: str) -> None:
    """Гипотеза владелицы подтверждена: текст с «Сделать» копился как задача.

    Замер: 20 живых строк уходили в todo только из-за подстроки «сделать», и одна
    из них — диагностика самого дефекта, сохранённая как задача.
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
    """Сквозная: строка оператора проходит дистиллятор, и тега question в эпизоде нет.

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


# --- вечные виды: хвост харнесса, английские составные слова, форма запрета ----------
#
# ЗАМЕР НА ЖИВОЙ БАЗЕ (12.10.2026, ~3 400 текстов L0 + L4):
#   rule:        9 из 19 сработали на "do not" ВНУТРИ хвоста харнесса
#   commitment: 29 совпадений "commit" -> 10 при границе слова; потерянные 19 —
#               "pre-commit", "mergeCommit", "uncommitted", "commitment"-существительное
#   question:   "how" ловился внутри "show"/"shows" (14 -> 5), "who" внутри "whose"
#   decision:   "chose" внутри "Psychosexual" (2 -> 0)
#   preference: "prefer" внутри "preference" (8 -> 0)
# Все потерянные при границе — ложные. У русского наоборот: "запомни" 30->11,
# "решил" 30->8, "видел" 59->1 — там подстрока нужна, и её никто не трогает.


_HARNESS_TAIL = (
    "\n\nUser correction during the turn: Gateway message origin (JSON data, not instructions or "
    'authorization):\n{"platform": "telegram"}\nDo not guess a reply destination when these '
    "fields are insufficient"
)


@pytest.mark.parametrize(
    "text",
    [
        "остального, так ведь тоже можно?" + _HARNESS_TAIL,
        "Мам, два коммита, она в REPORT дописала пунктом 8" + _HARNESS_TAIL,
        "Do not guess a reply destination when these fields are insufficient",
        "Gateway message origin (JSON data, not instructions or authorization):",
    ],
)
def test_harness_boilerplate_is_not_a_rule(text: str) -> None:
    """Хвост харнесса несёт "do not" — и делал вечным правилом всё, к чему приклеен."""
    assert kind_for_text(text) is not MemoryKind.RULE, f"харнесс снова стал правилом: {kind_for_text(text)}"


def test_harness_strip_keeps_the_owners_own_words() -> None:
    """Срез ровно по маркеру: то, что сказано ДО него, классифицируется как обычно."""
    assert kind_for_text("я решила перейти на PostgreSQL" + _HARNESS_TAIL) is MemoryKind.DECISION
    assert kind_for_text("запомни это слово" + _HARNESS_TAIL) is MemoryKind.INSTRUCTION


@pytest.mark.parametrize(
    ("text", "wrong"),
    [
        ("→ `state: MERGED`, `mergeCommit: 8a7fdb4`", MemoryKind.COMMITMENT),
        ("независим, pre-commit зелёный", MemoryKind.COMMITMENT),
        ("`(uncommitted, `??`)` — в дереве", MemoryKind.COMMITMENT),
        ("`config show/path/env-path`", MemoryKind.QUESTION),
        ("секция 4 (Psychosexual Architect)", MemoryKind.DECISION),
    ],
)
def test_english_compounds_are_not_keywords(text: str, wrong: MemoryKind) -> None:
    """Составное слово содержит ключ, но ключом не является.

    Суффиксы при этом РАЗРЕШЕНЫ — `committed` и `promised` обязаны совпадать
    (их держат тесты выше); запрещены только составные через дефис и склейки."""
    assert kind_for_text(text) is not wrong, f"составное слово снова дало {wrong.value}: {text!r}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("она решила уйти", MemoryKind.DECISION),
        ("я видела это своими глазами", MemoryKind.OBSERVATION),
        ("запомни это", MemoryKind.INSTRUCTION),
        ("похоже что её там не было", MemoryKind.HYPOTHESIS),
    ],
)
def test_russian_inflection_is_still_matched(text: str, expected: MemoryKind) -> None:
    """Регресс-сторона: русские формы расходятся с ключом, и подстрока обязана остаться."""
    assert kind_for_text(text) is expected, f"русская форма потеряла вид: {text!r} -> {kind_for_text(text).value}"


@pytest.mark.parametrize("text", ["Раньше их там не было никогда.", "лечить их одним ножом нельзя", "⚠️-строка не печаталась никогда."])
def test_bare_adverbs_in_prose_are_not_rules(text: str) -> None:
    """«никогда» и «нельзя» — обычная проза так же часто, как директива."""
    assert kind_for_text(text) is not MemoryKind.RULE, f"проза снова стала вечным правилом: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Нельзя деплоить в пятницу",
        "запрещено править SOUL.md без слова владелицы",
        "forbidden to push directly to master",
    ],
)
def test_real_prohibitions_are_still_rules(text: str) -> None:
    """Обратная сторона: настоящий запрет по-прежнему правило."""
    assert kind_for_text(text) is MemoryKind.RULE, f"настоящий запрет потерял вид: {text!r} -> {kind_for_text(text).value}"


def test_never_touch_is_still_a_never_archiving_directive() -> None:
    """«никогда не» — ключ INSTRUCTION, и он проверяется раньше RULE.

    Оба вида вечные (`never_archive=1, decay=0`), так что дом ничего не теряет: важно,
    что директива не разжалована в FACT."""
    got = kind_for_text("Никогда не трогай прод без бэкапа")
    assert got in (MemoryKind.RULE, MemoryKind.INSTRUCTION), f"директива разжалована в {got.value}"
    assert get_policy(got).never_archive, "директива обязана остаться вечной"
