"""The shadow bin must give back *the row that was asked for*, and refuse the rest.

Why this file exists (measured 2026-10-11). `restore_entries` used to match an
archived row on `original_id` alone — no owner, no kind. `original_id` is a number
shared by two tables that count independently (`episodes.episode_id` and
`core_memory.entry_id`), and the archive holds the kind in `memory_type`, which the
query never looked at. `fetchone()` took whichever came first, and the row was
written into `core_memory` regardless of what it had been.

**The overlap is per base, and the number must not be memorised.** From copies: the
`-cowagent` base held 931 id values in both tables and **108** `original_id` values
with two archived rows of different kinds for one owner; the `-hermes` base held 17
and **738**. An earlier version of this header quoted one pair as though it described
"the" base; it reproduced on neither, because there is no single base.

So these tests hold four promises, and the last two are the ones a reader should
be suspicious of:

  1. the owner and the kind each decide which row comes back;
  2. an episode returns to `episodes`, where it came from;
  3. a deliberately purged row is refused with a reason, never restored;
  4. the listing never offers what the restore refuses for that row's **reason or
     kind** — and it is not the stronger claim that the two are the same function.
     An id naming two rows of different kinds is shown, because the `memory_type`
     the listing returns is the argument that resolves it; `ambiguous` depends on
     the call, not on the row, so it cannot be pre-filtered.

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
    """A purged row is out of reach, and the reason is why. A silent 0 would look like an
    id that names nothing; the caller has to be able to tell the two apart.

    This docstring used to say "The 41% stay out of reach" — a share of one instance's
    archive, present-tense, in a file whose own header says a share of one base must not be
    memorised. It is no longer true of anything: those rows were deleted from Эли's base on
    2026-10-11 on her word, so the live bin holds 359 rows and **0** purges. The refusal
    stays because a row can carry such a reason again; the number never belonged here."""
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
    """A label with no restore path must never reach `core_memory` as a `memory_kind`.

    This test used to use `core_memory` as its example, on the reasoning that the
    value was a table name leaked into a kind field. That reasoning was invented and
    is now known to be false — `purge_foreign_rows.py` writes it deliberately for rows
    it selected from `core_memory`, so it routes there (see the test above). The
    example is `project_archive` instead, which really has nowhere to go: `project.py`
    archives a **wiki page** body, and this repository has no restore for one.
    """
    await _archive(cm, original_id=91, content="a wiki page's body", memory_type="project_archive", reason="project_archived")

    outcome = await _fs(cm).restore_entries([91], user_id=U1)

    assert outcome.restored == 0
    assert [r["reason"] for r in outcome.refused] == ["unroutable"]
    assert outcome.refused[0]["memory_type"] == "project_archive"
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
    assert listing["omitted_by_reason"] == {"purged": 1, "unroutable": 0}
    assert listing["restorable"] + listing["omitted"] == 2, "the disclosure must account for every row the user has"
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
    assert listing["omitted_by_reason"] == {"purged": 0, "unroutable": 0}, "the shape stays whole: zeros, not absent keys"


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


# ---------------------------------------------------------------------------
# The route, not the enum. `core_memory.memory_kind` is a bare `TEXT` with no
# CHECK; one write path coerces it through `MemoryKind`, but the column does not,
# and the bases hold kinds the enum has never heard of — `log` and `bug`, both
# measured 2026-10-11. An allowlist built from the enum therefore refuses rows
# that legitimately came out of `core_memory`, which is a worse defect than the
# one this file was written to repair: a false refusal looks like a rule.
#
# The route table replaces it, and it is a denylist rather than an allowlist on
# purpose. What can be *named* is small and closed — the sources that are not
# `core_memory` — while what can be a core kind is open, because nothing
# constrains it. Closing the open side is what the enum did wrong.
# ---------------------------------------------------------------------------


async def test_a_kind_the_enum_does_not_know_still_goes_back(cm: Any) -> None:
    """`log` was a real row in `core_memory` and must come back as one. Measured:
    1 `log` row and 3 `bug` rows exist in the live bases, in `core_memory`."""
    await _archive(cm, original_id=301, content="logged=1", memory_type="log")

    outcome = await _fs(cm).restore_entries([301], user_id=U1, memory_type="log")

    assert outcome.restored == 1, "a kind the enum does not list is not ipso facto unroutable"
    assert [r["value"] for r in await _rows(cm, "SELECT value FROM core_memory WHERE key='logged'")] == ["1"]


async def test_a_row_whose_kind_was_never_recorded_is_refused_not_guessed(cm: Any) -> None:
    """`NULL` means the writer did not say, and the one production caller that leaves
    it empty archives rows that were never in `core_memory` at all: `rag/conflict.py`
    archives `memory_conflicts`. Substituting `fact` is the same guess `fetchone()`
    was making, so it is refused instead.

    An earlier version of this docstring said three writers left it empty and named
    `forget`'s graph branch as one of them. Neither was true — the graph branch passes
    `graph:<node_type>`, and `rag/conflict.py` is the only caller that omits the
    argument. Found by reading every call site rather than by counting a grep.
    """
    await _archive(cm, original_id=311, content="conflict_9: something", memory_type=None, reason="conflict_resolved")

    outcome = await _fs(cm).restore_entries([311], user_id=U1)

    assert outcome.restored == 0, "a row with no recorded kind was written into core_memory anyway"
    assert [r["reason"] for r in outcome.refused] == ["unroutable"]
    written = await _rows(cm, "SELECT COUNT(*) FROM core_memory WHERE key LIKE 'conflict%'")
    assert written[0][0] == 0, "the conflict row was filed as an L4 key on a guessed kind"


async def test_a_graph_row_is_refused_rather_than_filed_as_a_key(cm: Any) -> None:
    """`graph:<node_type>` is written by `forget`'s shadow bin for the audit trail;
    there is no restore into `graph_nodes`, so the row must not be written into
    `core_memory` as though it had been one."""
    await _archive(cm, original_id=321, content="a node's text", memory_type="graph:fact", reason="forget_primitive_fuzzy_graph")

    outcome = await _fs(cm).restore_entries([321], user_id=U1)

    assert outcome.restored == 0
    assert [r["reason"] for r in outcome.refused] == ["unroutable"]
    assert [r["memory_type"] for r in outcome.refused] == ["graph:fact"]


async def test_a_project_archive_row_is_refused_rather_than_filed_as_a_key(cm: Any) -> None:
    """`project_archive` comes from a **wiki page** (`project.py` deletes the file),
    not from `core_memory`. It was restored into `core_memory` before this test."""
    await _archive(cm, original_id=0, content="a wiki page's body", memory_type="project_archive", reason="project_archived")

    outcome = await _fs(cm).restore_entries([0], user_id=U1)

    assert outcome.restored == 0, "a wiki page was filed as an L4 key"
    assert [r["reason"] for r in outcome.refused] == ["unroutable"]


async def test_the_listing_hides_what_the_restore_would_refuse_beyond_purges(cm: Any) -> None:
    """Her rule is about what `undo` can act on, and a row with no route is not
    something it can act on — so it is hidden too, and counted."""
    await _archive(cm, original_id=401, content="fine=1", memory_type="fact")
    await _archive(cm, original_id=402, content="conflict_1: x", memory_type=None, reason="conflict_resolved")
    await _archive(cm, original_id=403, content="a node", memory_type="graph:fact")

    listing = await _am(cm).get_archived(U1)

    assert [e["original_id"] for e in listing["entries"]] == [401]
    assert listing["omitted"] == 2


async def test_the_listing_declares_each_reason_separately(cm: Any) -> None:
    """One total hides which rule hid what, and the two rules are not the same
    decision: a purge is deliberate removal, an unroutable kind is a missing fact."""
    purged = min(PURGE_REASONS)
    await _archive(cm, original_id=411, content="fine=1", memory_type="fact")
    await _archive(cm, original_id=412, content="p", memory_type="episode", reason=purged)
    await _archive(cm, original_id=413, content="g", memory_type="graph:fact")
    await _archive(cm, original_id=414, content="c", memory_type=None, reason="conflict_resolved")

    listing = await _am(cm).get_archived(U1)

    assert listing["omitted_by_reason"] == {"purged": 1, "unroutable": 2}
    assert listing["omitted"] == 3


async def test_the_page_is_not_the_bin(cm: Any) -> None:
    """`shown` is a page size, `omitted` is a fact about the bin, and the two must
    not be added together — the earlier docstring said `shown + omitted` was
    everything the user has, which is false as soon as the page is truncated."""
    for n in range(3):
        await _archive(cm, original_id=500 + n, content=f"k{n}=v{n}", memory_type="fact")
    await _archive(cm, original_id=599, content="p", memory_type="episode", reason=min(PURGE_REASONS))

    listing = await _am(cm).get_archived(U1, limit=2)

    assert listing["shown"] == 2, "the limit was not honoured"
    assert listing["restorable"] == 3, "the arithmetic must count the bin, not the page"
    assert listing["omitted"] == 1
    assert listing["restorable"] + listing["omitted"] == 4, "the true disclosure: every row this user has"


async def test_the_listing_shows_an_ambiguous_row_because_its_kind_resolves_it(cm: Any) -> None:
    """The listing is not simply 'what the restore accepts without arguments'.
    One id over two kinds is refused when the kind is unnamed and accepted when it
    is named — and the listing now returns `memory_type`, which is exactly the
    argument that resolves it. Hiding it would throw away a row that can be had."""
    await _archive(cm, original_id=601, content="chosen_colour=green", memory_type="fact")
    await _archive(cm, original_id=601, content="an episode summary", memory_type="episode")

    listing = await _am(cm).get_archived(U1)
    offered = sorted(e["memory_type"] for e in listing["entries"])

    assert offered == ["episode", "fact"], "an ambiguous id was hidden, though the kind it returns resolves it"

    blind = await _fs(cm).restore_entries([601], user_id=U1)
    assert [r["reason"] for r in blind.refused] == ["ambiguous"]

    named = await _fs(cm).restore_entries([601], user_id=U1, memory_type="fact")
    assert named.restored == 1, "the kind the listing returns did not resolve the ambiguity"


async def test_a_restored_episode_keeps_a_recorded_zero_weight(cm: Any) -> None:
    """`0.0` is a value the archive recorded, not an absent one.

    The first version of `_restore_episode` passed `float(row["importance"] or 0.5)`,
    which reads a recorded `0.0` as missing and hands the episode back heavier than it
    was. This docstring first called the case latent, on a measurement of one base;
    the `-hermes` base holds **one** such row (an episode, `importance=0.0`, reason
    `forget_primitive_fuzzy_l3`, measured 2026-10-11), so the old code would have
    restored that episode at 0.5 instead of 0.0. It was live, not latent — which is
    the difference between one base and all of them.
    """
    await _archive(cm, original_id=801, content="a silent episode", memory_type="episode", importance=0.0)

    outcome = await _fs(cm).restore_entries([801], user_id=U1)

    assert outcome.restored == 1
    weights = await _rows(cm, "SELECT emotional_weight FROM episodes WHERE user_id=?", (U1,))
    assert [float(r[0]) for r in weights] == [0.0], "a recorded zero weight was replaced by the default"


async def test_a_restored_episode_with_no_recorded_weight_gets_the_default(cm: Any) -> None:
    """The other side of the same line: `NULL` really is absent, and the default is
    the honest answer there. Both cases are asserted so the fix cannot be mistaken
    for 'always 0.5' or for 'never 0.5'."""
    await _archive(cm, original_id=811, content="a weighted episode", memory_type="episode", importance=None)

    outcome = await _fs(cm).restore_entries([811], user_id=U1)

    assert outcome.restored == 1
    weights = await _rows(cm, "SELECT emotional_weight FROM episodes WHERE user_id=?", (U1,))
    assert [float(r[0]) for r in weights] == [0.5], "an absent weight did not fall back to the default"


async def test_a_restored_core_row_without_a_separator_keeps_its_text(cm: Any) -> None:
    """`partition("=")` on content with no `=` gives an empty value. Writing the whole
    text into `value` as well is a fallback, and this pins that it is deliberate —
    the alternative (an empty value) loses the text entirely.

    "No row lacks a `=`" is true of the rows routed to `core_memory`, and false of
    the archive as a whole: episode summaries are bare text and 1324 of them in the
    `-hermes` base contain no `=` (measured 2026-10-11). The first version of this
    docstring said "no routable row", which counted the episodes in and made the
    sentence wrong — routed to `episodes` is also routable."""
    await _archive(cm, original_id=821, content="a bare sentence with no separator", memory_type="fact")

    outcome = await _fs(cm).restore_entries([821], user_id=U1)

    assert outcome.restored == 1
    row = await _rows(cm, "SELECT key, value FROM core_memory WHERE user_id=?", (U1,))
    assert row[0][0] == "a bare sentence with no separator"
    assert row[0][1] == "a bare sentence with no separator", "the text was dropped instead of kept whole"


async def test_a_row_labelled_with_its_source_table_routes_there(cm: Any) -> None:
    """`episode` names the table `episodes` and routes there. `core_memory` names
    the table `core_memory`, and the value is written by `scripts/purge_foreign_rows.py`
    for rows it selected **from** `core_memory` — so the same convention applies.

    The first version of `route_of` refused it, on a story that turned out to be
    invented: that the value was a table name which had leaked into a kind field.
    It had not. The label is deliberate and it names the source; the reason these
    rows are not recoverable in practice is `archive_reason`, not the route.

    What the label does **not** carry is the row's original kind, so the restored
    row keeps the label as its kind — a kind the registry does not list, which is
    visible rather than disguised as `fact`.
    """
    await _archive(cm, original_id=901, content="purged_key=purged_value", memory_type="core_memory", reason="inactive_90d")

    outcome = await _fs(cm).restore_entries([901], user_id=U1)

    assert outcome.restored == 1, "a row whose label names its source table was called unroutable"
    row = await _rows(cm, "SELECT key, value, memory_kind FROM core_memory WHERE user_id=?", (U1,))
    assert row[0][0] == "purged_key" and row[0][1] == "purged_value"
    # The consequence, asserted rather than left to be discovered: the label names the
    # source, not the kind, so the restored row carries a kind the registry does not
    # list. `validate_kind` rejects it and `decay_importance` skips such a row, so it
    # will not decay. That is a real difference from a `fact` — and a visible one,
    # which is the point of writing what was recorded instead of substituting a default.
    assert row[0][2] == "core_memory"


async def test_the_listing_never_offers_what_the_restore_refuses(cm: Any) -> None:
    """The anti-drift test in the direction that matters, asked of every row
    rather than of a hand-picked pair: for each row the listing shows, a restore
    must not refuse it for its reason or its kind; for each row it hides, a restore
    must refuse it. An `ambiguous` refusal is allowed on a shown row — that is the
    one case the kind the listing returns is there to resolve."""
    purged = min(PURGE_REASONS)
    await _archive(cm, original_id=701, content="a=1", memory_type="fact")
    await _archive(cm, original_id=702, content="b=2", memory_type="log")
    await _archive(cm, original_id=703, content="c=3", memory_type="episode", reason=purged)
    await _archive(cm, original_id=704, content="d=4", memory_type=None, reason="conflict_resolved")
    await _archive(cm, original_id=705, content="e=5", memory_type="graph:fact")

    listing = await _am(cm).get_archived(U1)
    fs = _fs(cm)

    for entry in listing["entries"]:
        outcome = await fs.restore_entries([entry["original_id"]], user_id=U1, memory_type=entry["memory_type"])
        assert outcome.refused == [], f"the listing offered id {entry['original_id']} that the restore refuses: {outcome.refused}"

    hidden = {703, 704, 705}
    for original_id in hidden:
        outcome = await fs.restore_entries([original_id], user_id=U1)
        assert outcome.refused, f"the listing hid id {original_id}, but the restore accepts it"
