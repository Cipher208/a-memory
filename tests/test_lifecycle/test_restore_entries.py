"""The shadow bin must give back *the row that was asked for*, and refuse the rest.

Why this file exists (measured 2026-10-11). `restore_entries` used to match an
archived row on `original_id` alone — no owner, no kind. `original_id` is a number
shared by two tables that count independently (`episodes.episode_id` and
`core_memory.entry_id`): 870 id values existed in both, and 10 `original_id` values
already carried two archived rows of different kinds. `fetchone()` took whichever
came first and wrote it into `core_memory` regardless of what it had been.

So these tests hold four promises, and the last two are the ones a reader should
be suspicious of:

  1. the owner and the kind each decide which row comes back;
  2. an episode returns to `episodes`, where it came from;
  3. a deliberately purged row is refused with a reason, never restored;
  4. the listing and the restore answer the same question — every id the listing
     hands out is an id the restore accepts, and none it omits is one it would take.

Fixtures are synthetic throughout: no `original_id`, summary or owner value is
copied out of a live base.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from lifecycle.forgetting import ForgettingSystem
from shared.archived_memories import PURGE_REASONS, ArchivedMemories
from shared.connection import connection_manager
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

U1 = "u_restore_1"
U2 = "u_restore_2"


@pytest.fixture
async def cm(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """A really migrated database: this repair is a claim about a WHERE clause."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


def _fs(cm: Any, layer: str = "user") -> ForgettingSystem:
    return ForgettingSystem(cm=cm, layer=layer)


def _am(cm: Any) -> ArchivedMemories:
    """The store, unprimed on purpose: `archive()` and `get_archived()` both call
    `ensure()` themselves, so nothing has to be created here."""
    return ArchivedMemories(cm=cm)


async def _archive(
    cm: Any,
    *,
    original_id: int | None,
    content: str,
    memory_type: str | None = "fact",
    reason: str = "inactive_90d",
    user_id: str = U1,
    importance: float = 0.5,
) -> int:
    store = _am(cm)
    return await store.archive(
        user_id=user_id,
        content=content,
        memory_type=memory_type,
        importance=importance,
        original_id=original_id,
        reason=reason,
    )


async def _rows(cm: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    conn = await cm.get("memory.db")
    return list(await (await conn.execute(sql, params)).fetchall())


async def _archived_count(cm: Any, original_id: int) -> int:
    rows = await _rows(cm, "SELECT COUNT(*) FROM archived_memories WHERE original_id = ?", (original_id,))
    return int(rows[0][0])


async def test_the_kind_decides_which_row_comes_back(cm: Any) -> None:
    """One number, two archived rows. Only the kind can tell them apart."""
    await _archive(cm, original_id=7, content="an episode, stored as a bare summary", memory_type="episode", reason="inactive_90d")
    await _archive(cm, original_id=7, content="chosen_colour=green", memory_type="fact", reason="expired")

    outcome = await _fs(cm).restore_entries([7], user_id=U1, memory_type="fact")

    assert outcome.restored == 1
    assert not outcome.refused
    keys = [r["key"] for r in await _rows(cm, "SELECT key FROM core_memory WHERE user_id = ?", (U1,))]
    assert keys == ["chosen_colour"]
    assert await _archived_count(cm, 7) == 1, "the episode row was consumed as well — the match is still one field wide"


async def test_the_owner_decides_which_row_comes_back(cm: Any) -> None:
    """The same number under two owners: one comes back, the other is untouched."""
    await _archive(cm, original_id=11, content="mine=1", user_id=U1)
    await _archive(cm, original_id=11, content="theirs=2", user_id=U2)

    outcome = await _fs(cm).restore_entries([11], user_id=U1)

    assert outcome.restored == 1
    values = [r["value"] for r in await _rows(cm, "SELECT value FROM core_memory WHERE user_id = ?", (U1,))]
    assert values == ["1"]
    assert await _archived_count(cm, 11) == 1, "the other owner's row was consumed"


async def test_an_episode_returns_to_episodes_and_not_to_core_memory(cm: Any) -> None:
    """Routing before parsing. The archive keeps an episode's summary bare, so
    filing it as `key=value` would have invented a key out of its first words."""
    await _archive(cm, original_id=21, content="we shipped the repair on friday", memory_type="episode", importance=0.9)

    outcome = await _fs(cm).restore_entries([21], user_id=U1)

    assert outcome.restored == 1
    episodes = await _rows(cm, "SELECT summary, emotional_weight, user_id FROM episodes")
    assert len(episodes) == 1
    assert episodes[0]["summary"] == "we shipped the repair on friday"
    assert episodes[0]["emotional_weight"] == pytest.approx(0.9)
    assert episodes[0]["user_id"] == U1
    assert await _rows(cm, "SELECT entry_id FROM core_memory") == [], "an episode was filed as an L4 fact"
    assert await _archived_count(cm, 21) == 0


async def test_a_restored_episode_loses_what_the_archive_never_kept(cm: Any) -> None:
    """The limit, asserted rather than left to be discovered: the archive holds a
    summary and a weight, so `tags`, `created_at` and `layer` cannot come back.
    A restored episode carries the moment of its return."""
    await _archive(cm, original_id=31, content="an old moment", memory_type="episode", importance=0.4)

    before = time.time()
    await _fs(cm, layer="agent").restore_entries([31], user_id=U1)

    row = (await _rows(cm, "SELECT tags, created_at, layer FROM episodes"))[0]
    assert row["tags"] == "[]", "tags came back from somewhere — the archive does not hold them"
    assert before <= row["created_at"] <= time.time() + 1, "created_at is the moment of return, not an archived value"
    assert row["layer"] == "agent", "the restore must land in the system's own layer, the only one it has"


async def test_an_episode_whose_text_was_re_added_is_still_counted_as_present(cm: Any) -> None:
    """`EpisodicMemory.save()` dedups on (layer, user_id, summary). Restoring text
    that is already there is a no-op on rows and a success for the caller — a
    deliberate reading, asserted so it cannot drift into an accident."""
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at) VALUES ('user', ?, 'same words', 0.5, '[]', 1.0)",
        (U1,),
    )
    await conn.commit()
    await _archive(cm, original_id=41, content="same words", memory_type="episode")

    outcome = await _fs(cm).restore_entries([41], user_id=U1)

    assert outcome.restored == 1
    assert len(await _rows(cm, "SELECT episode_id FROM episodes WHERE summary='same words'")) == 1
    assert await _archived_count(cm, 41) == 0, "the archive row must still be consumed — the restore did succeed"


async def test_a_purged_row_is_refused_with_a_reason(cm: Any) -> None:
    """The 41% stay out of reach. A silent 0 would look like an id that names
    nothing; the caller has to be able to tell the two apart."""
    purged = min(PURGE_REASONS)
    await _archive(cm, original_id=51, content="removed on purpose", memory_type="episode", reason=purged)

    outcome = await _fs(cm).restore_entries([51], user_id=U1)

    assert outcome.restored == 0
    assert [r["reason"] for r in outcome.refused] == ["purged"]
    assert outcome.not_found == [], "a refusal is not a miss"
    assert await _archived_count(cm, 51) == 1, "the row was consumed by a refusal"


async def test_a_purged_row_is_refused_even_when_the_kind_is_named(cm: Any) -> None:
    """The ruling is that this is not restorable through the primitive *ever* —
    naming both the id and the kind is not a way in, and there is no parameter
    that turns the refusal off."""
    purged = min(PURGE_REASONS)
    await _archive(cm, original_id=61, content="removed on purpose", memory_type="episode", reason=purged)

    outcome = await _fs(cm).restore_entries([61], user_id=U1, memory_type="episode")

    assert outcome.restored == 0
    assert [r["reason"] for r in outcome.refused] == ["purged"]
    assert await _rows(cm, "SELECT episode_id FROM episodes") == []


async def test_a_value_holding_its_own_equals_sign_survives(cm: Any) -> None:
    """`content` is `key=value`, so the split must take the first separator only."""
    await _archive(cm, original_id=71, content="connection_string=host=db port=5432")

    outcome = await _fs(cm).restore_entries([71], user_id=U1)

    assert outcome.restored == 1
    row = (await _rows(cm, "SELECT key, value FROM core_memory"))[0]
    assert row["key"] == "connection_string"
    assert row["value"] == "host=db port=5432"


async def test_an_ambiguous_id_is_refused_rather_than_guessed(cm: Any) -> None:
    """Two rows, one number, one owner, no kind named. The old code picked the
    first; the repair refuses. This is the behaviour change, in one test."""
    await _archive(cm, original_id=81, content="a=1", memory_type="fact")
    await _archive(cm, original_id=81, content="a=2", memory_type="question")

    outcome = await _fs(cm).restore_entries([81], user_id=U1)

    assert outcome.restored == 0
    assert [r["reason"] for r in outcome.refused] == ["ambiguous"]
    assert sorted(outcome.refused[0]["kinds"]) == ["fact", "question"]
    assert await _archived_count(cm, 81) == 2
    assert await _rows(cm, "SELECT entry_id FROM core_memory") == []


async def test_an_id_that_names_nothing_is_reported(cm: Any) -> None:
    outcome = await _fs(cm).restore_entries([999_999], user_id=U1)

    assert outcome.restored == 0
    assert outcome.not_found == [999_999]
    assert outcome.refused == []


async def test_an_unroutable_kind_is_refused_instead_of_written(cm: Any) -> None:
    """A table name leaked into a kind field must never reach `core_memory` as a
    `memory_kind` — an invalid kind is a row that later reads cannot classify."""
    await _archive(cm, original_id=91, content="x=1", memory_type="core_memory", reason="test")

    outcome = await _fs(cm).restore_entries([91], user_id=U1)

    assert outcome.restored == 0
    assert [r["reason"] for r in outcome.refused] == ["unsupported_kind"]
    assert outcome.refused[0]["memory_type"] == "core_memory"
    assert await _rows(cm, "SELECT entry_id FROM core_memory") == []


async def test_an_empty_id_list_does_nothing(cm: Any) -> None:
    outcome = await _fs(cm).restore_entries([], user_id=U1)

    assert outcome.restored == 0
    assert outcome.refused == []
    assert outcome.not_found == []


async def test_the_listing_omits_a_purged_row_and_declares_the_count(cm: Any) -> None:
    """Her ruling, 2026-10-11: hide the purge rows, and say how many were hidden.
    The count is asserted, because an omission whose arithmetic is untested is how
    the disclosure quietly stops being true."""
    purged = min(PURGE_REASONS)
    await _archive(cm, original_id=101, content="restorable=1", reason="inactive_90d")
    await _archive(cm, original_id=102, content="purged=1", memory_type="episode", reason=purged)

    listing = await _am(cm).get_archived(U1)

    assert [e["original_id"] for e in listing["entries"]] == [101]
    assert listing["shown"] == 1
    assert listing["omitted"] == 1
    assert listing["omitted_reason"] == "purge"
    assert listing["shown"] + listing["omitted"] == 2, "the disclosure must account for every row the user has"
    assert await _archived_count(cm, 102) == 1, "hidden is not deleted — the row must still be in the table"


async def test_the_listing_carries_what_is_needed_to_ask_for_a_row_back(cm: Any) -> None:
    """Without `memory_type` and `original_id` an agent cannot name the row it
    wants, which made the bin unusable rather than merely full."""
    await _archive(cm, original_id=111, content="named=1", memory_type="fact")

    entry = (await _am(cm).get_archived(U1))["entries"][0]

    assert entry["memory_type"] == "fact"
    assert entry["original_id"] == 111


async def test_the_listing_stays_quiet_when_it_hid_nothing(cm: Any) -> None:
    """A reason for an omission that did not happen is its own small lie."""
    await _archive(cm, original_id=121, content="only=1")

    listing = await _am(cm).get_archived(U1)

    assert listing["omitted"] == 0
    assert listing["omitted_reason"] is None


async def test_the_listing_and_the_restore_answer_the_same_question(cm: Any) -> None:
    """The anti-drift test, and the reason the listing was filtered here rather
    than in the `undo` that will stand on top of it: a listing that advertises
    what the restore refuses is worse than either rule alone."""
    purged = min(PURGE_REASONS)
    await _archive(cm, original_id=131, content="first=1", memory_type="fact", reason="inactive_90d")
    await _archive(cm, original_id=132, content="second=2", memory_type="fact", reason="expired")
    await _archive(cm, original_id=133, content="hidden=3", memory_type="episode", reason=purged)

    listing = await _am(cm).get_archived(U1)
    offered = [e["original_id"] for e in listing["entries"]]
    assert sorted(offered) == [131, 132], "the listing offered a row it should have hidden"

    accepted = await _fs(cm).restore_entries(offered, user_id=U1)
    assert accepted.restored == len(offered), "the listing offered an id the restore would not take"
    assert accepted.refused == []
    assert accepted.not_found == []

    refused = await _fs(cm).restore_entries([133], user_id=U1)
    assert [r["reason"] for r in refused.refused] == ["purged"], "an id the listing hid was accepted by the restore"
