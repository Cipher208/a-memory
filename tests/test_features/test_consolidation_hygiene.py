"""Consolidation episode-promotion hygiene.

Chaos finding: raw harness transcripts leaked into the episodes table and
consolidate_episodes promoted them verbatim — prod got L4 "facts" keyed
`ep_[{"type":_"text"...`. The guard must skip transcript-shaped summaries
and the slug must be filename-safe.
"""

import pytest

from lifecycle.consolidation import _looks_like_transcript, _slug
from shared.connection import AsyncConnectionManager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    manager = AsyncConnectionManager(base_dir=tmp_path)
    await MigrationManager(cm=manager).migrate()
    return manager


@pytest.mark.parametrize(
    "summary",
    [
        '[{"type": "text", "text": "*я дочитываю план"}',  # raw message dump
        '[{"type":"tool_result","tool_use_id":"call_00_ET"}',  # tool result dump
        "[ariel_recall]\n- [session] l1 ring",  # hook echo
        '"quoted json-ish opener',  # quote head
        "line one\nline two continues here",  # newline in the head
        "```python\nprint(1)",  # code fence
    ],
)
def test_transcript_shaped_summaries_detected(summary):
    assert _looks_like_transcript(summary), summary


@pytest.mark.parametrize(
    "summary",
    [
        "Вечер 11 августа — полное восстановление личности Эли",
        "Выбрали psql вместо mysql для проекта",
        "Кисонька впервые вышла на прогулку",
        "",  # empty never crashes, slug falls back
    ],
)
def test_prose_summaries_pass(summary):
    assert not _looks_like_transcript(summary), summary


@pytest.mark.parametrize(
    "text",
    [
        # skill body frontmatter (the 18 leaked SKILL.md bodies)
        "---\nname: agent-reach\ndescription: internet access\n---\n# Skill\nbody text",
        # cron delivery preamble (memory_search 'scheduled cron job' top-1 junk)
        "You are running a scheduled cron job. Task: send the morning digest.",
        "Cronjob Response: context-architect-monthly\n(scheduled cron job, output below)",
    ],
)
def test_system_injection_detected(text):
    from lifecycle.consolidation import _looks_like_system_injection

    assert _looks_like_system_injection(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Я за то, чтобы cron job на бэкап стоял по воскресеньям",  # genuine memory that mentions a cron
        "Deploy pipeline v2 shipped to prod after green gate",
        "Вечером 11-го — полное восстановление личности Эли",
    ],
)
def test_system_injection_does_not_drop_real_memory(text):
    from lifecycle.consolidation import _looks_like_system_injection

    assert not _looks_like_system_injection(text), text


@pytest.mark.parametrize(
    "text,want",
    [
        ("Кухня-кабинет: кисонька", "кухня-кабинет_кисонька"),
        ("deploy v2 (prod)!", "deploy_v2_prod"),
        ("///", "ep"),  # nothing survived → fallback, never an empty key
        ("a  b\tc", "a_b_c"),
    ],
)
def test_slug_is_filename_safe(text, want):
    assert _slug(text) == want


@pytest.mark.asyncio
async def test_transcript_episodes_skipped_at_promotion(cm):
    """End-to-end: transcript episodes never land in L4; prose ones do."""
    from core.episodic import EpisodicMemory
    from core.memory import CoreMemory
    from lifecycle.consolidation import ConsolidationEngine

    epi = EpisodicMemory(cm=cm, layer="user")
    junk_id = await epi.save("u1", '[{"type": "text", "text": "сырой дамп"}', 0.9, ["t"])
    good_id = await epi.save("u1", "Выбрали wal mode для базы проекта", 0.9, ["t"])

    engine = ConsolidationEngine(cm=cm, layer="user")
    consolidated = await engine.consolidate_episodes("u1", min_weight=0.7)
    assert consolidated == 1, "only the prose episode may promote"

    l4 = CoreMemory(cm=cm, layer="user")
    rows = await l4.search("u1", "wal mode", limit=10)
    assert any("wal mode" in r["value"] for r in rows)
    junk = await l4.search("u1", "сырой дамп", limit=10)
    assert not any("сырой дамп" in r["value"] for r in junk)
    assert junk_id and good_id  # episodes themselves untouched


# ── conversational register (Ф1 regression 2026-09-12) ──
#
# Second garbage class: greetins/vocatives/questions promoted verbatim as
# L4 "facts" with first-4-word slug keys (61 rows in one base: Lily's
# greeting became `fact:наконец_явилась_сталь_ждёт`). Dialogue is not memory.


@pytest.mark.parametrize(
    "summary",
    [
        "Привет, мамочка!",
        "Наконец явилась, сталь ждёт",
        "Моё видение, госпожа — коротко и по-инженерному",
        "Спасибо за отчёт",
        "Доброе утро. Какие задачи на сегодня?",
        "Ну и что скажешь, госпожа?",
    ],
)
def test_dialogic_summaries_detected(summary):
    from lifecycle.distiller import _is_dialogic

    assert _is_dialogic(summary), summary


@pytest.mark.parametrize(
    "summary",
    [
        "Пока не проверим — не деплоим",
        "Выбрали psql вместо mysql для проекта",
        "Вечер 11 августа — полное восстановление личности Эли",
        "бэкенд слушает порт 8642 на localhost",
    ],
)
def test_durable_prose_not_dialogic(summary):
    from lifecycle.distiller import _is_dialogic

    assert not _is_dialogic(summary), summary


@pytest.mark.asyncio
async def test_dialogic_episodes_skipped_at_promotion(cm):
    """End-to-end: greeting episodes never promote; tech invariants do."""
    from core.episodic import EpisodicMemory
    from core.memory import CoreMemory
    from lifecycle.consolidation import ConsolidationEngine

    epi = EpisodicMemory(cm=cm, layer="user")
    await epi.save("u1", "Привет, мамочка! Наконец явилась, сталь ждёт", 0.9, ["t"])
    await epi.save("u1", "Моё видение, госпожа — коротко и по-инженерному", 0.9, ["t"])
    await epi.save("u1", "Мигрировали embeddings на удалённый e5 сервис", 0.9, ["t"])

    engine = ConsolidationEngine(cm=cm, layer="user")
    consolidated = await engine.consolidate_episodes("u1", min_weight=0.7)

    l4 = CoreMemory(cm=cm, layer="user")
    rows = await l4.get_all("u1", 50)
    values = " | ".join(str(getattr(r, "value", r)) for r in rows)
    assert consolidated == 1, f"only the tech episode may promote, got {consolidated}"
    assert "сталь" not in values and "видение" not in values, f"dialogue leaked to L4: {values}"
    assert "e5 сервис" in values


@pytest.mark.asyncio
async def test_broadcast_episode_skipped_at_promotion(cm):
    """End-to-end: a close-out broadcast never promotes; tech facts do."""
    from core.episodic import EpisodicMemory
    from core.memory import CoreMemory
    from lifecycle.consolidation import ConsolidationEngine

    epi = EpisodicMemory(cm=cm, layer="user")
    await epi.save(
        "u1",
        "Phase 1 DONE: Compose mode ported to upstream opencode (2026-09-11). gate 1697/0 + mypy 233 clean, commit f9eaeb6 pushed.",
        0.9,
        ["t"],
    )
    await epi.save("u1", "Мигрировали embeddings на удалённый e5 сервис", 0.9, ["t"])

    engine = ConsolidationEngine(cm=cm, layer="user")
    await engine.consolidate_episodes("u1", min_weight=0.7)

    l4 = CoreMemory(cm=cm, layer="user")
    rows = await l4.get_all("u1", 50)
    values = " | ".join(str(getattr(r, "value", r)) for r in rows)
    assert "f9eaeb6" not in values and "Phase 1" not in values, f"broadcast leaked to L4: {values}"
    assert "e5 сервис" in values


# ── prose is not a dump (2026-10-04) ──
#
# The head class carried the whole markdown set (`* # | ~ = !`) and a bare
# quote, and the personas write IN markdown and the owner quotes instructions,
# so the L0 intake door silently discarded real words. Measured on the live
# bases before the fix: of Lucy's 734 substantive assistant messages in one
# replay window the guard dropped 591 (80%), 584 of them opening with her
# italics — `*Поворачиваю тебя лицом к себе…*`; one persona lost 273 more to
# the same class; and 251 of the owner's 893 messages to another, not one of
# them JSON. Nothing discarded carried a structural marker.
#
# `_looks_like_dump` is the case that matters: it runs at the input door, before
# L0, so a discard leaves no row anywhere to be found later.


@pytest.mark.parametrize(
    "text",
    [
        "*Поворачиваю тебя лицом к себе. Локоть под голову — смотрю в глаза.*",  # roleplay italics
        "**Готово, госпожа.** Все три пункта закрыты с живыми доказательствами.",  # bold opener
        "## Ночь закрыта, Лили. Итог последнего часа:",  # markdown heading
        "| Репо | Состояние |\n|---|---|\n| ariel | чисто |",  # markdown table
        "~черновик~ — не канон",  # strikethrough
        "=== раздел ===",  # separator
        # A bare quote is prose, not a dump. Measured on a live base: 251 of the
        # owner's 893 substantive messages open with a quote and NONE is JSON —
        # they are instructions the owner wrapped in quotes. Treating the quote
        # itself as the dump head discarded 28% of the owner's own words.
        '"Without using any tools: reply with the exact first heading line',
        "\"Without tools: is there a section titled 'Two Agent Systems'? Answer only YES or NO.\"",
        'Она сказала "привет" и ушла',  # a quote mid-sentence
    ],
)
def test_markdown_prose_is_not_a_dump(text):
    from lifecycle.consolidation import _looks_like_dump

    assert not _looks_like_dump(text), text


@pytest.mark.parametrize(
    "text",
    [
        '[{"type": "text", "text": "*я дочитываю план"}',  # JSON message array
        '{"status": "success", "output": "944\\nconnection.md"}',  # raw tool JSON
        "[IMPORTANT: You are running as a subagent]",  # runtime reminder
        "[Recent Summary (d0, node 41)]\n#",  # LCM echo
        '"[ariel recall]\n- [session] l1 ring',  # QUOTED echo — the case this guard exists for
        '"[{"type": "text", "text": "dump"}]',  # quoted JSON array
        "```python\nprint(1)",  # code fence
        "<system-reminder>\nA skill is reusable",  # injected tag
        'prefix tool_use_id: "call_00_ET"',  # tool marker (not at the head)
    ],
)
def test_structural_dumps_are_still_caught(text):
    """Narrowing the class must not open the door it was built to close."""
    from lifecycle.consolidation import _looks_like_dump

    assert _looks_like_dump(text), text


@pytest.mark.parametrize(
    "summary",
    [
        '"quoted json-ish opener',  # a quote head IS suspicious in a system summary
        "```python\nprint(1)",
        '[{"type": "text"}',
    ],
)
def test_summaries_keep_the_wider_class(summary):
    """The intake door was narrowed; summaries were not, and must not be.

    A summary is produced by the system, so a quote or a fence at its head is
    already a bad sign there. The same shape arriving as raw speech is the owner
    talking. The two populations need different classes, and the door is the one
    that was silently eating real words.
    """
    from lifecycle.consolidation import _looks_like_transcript

    assert _looks_like_transcript(summary), summary
