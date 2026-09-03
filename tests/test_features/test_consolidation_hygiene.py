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
