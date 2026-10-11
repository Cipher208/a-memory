"""`forget(scope="undo")` must give an archived row back — and say what it could not.

Why this file exists. PRs #98 and #99 made the bin **readable and correct**, but nothing
an agent can call reads it: `get_archived()` has no tool of its own, and `restore_entries`
is reachable only through `memory_proposals(action="revert")`, which a caller uses on a
proposal it staged, not on an id it chose. So the requested capability is still absent at
the surface. `undo` is that surface, and it is the last piece of the Discussion #1 request.

The promises held here, and the fourth is the one a reader should be suspicious of:

  1. an episode comes back into `episodes` and an L4 row into `core_memory`, routed by the
     kind the archive recorded — the archive row is consumed, not copied;
  2. `dry_run=True` writes nothing anywhere and still names each row it would return;
  3. a refusal is a refusal with a reason, and it is **not** reported as "nothing there":
     a purged id, an id with no recorded kind and an id that never existed are three
     different answers, and collapsing them is the silence PR #99 exists to end;
  4. **the archive is not a backup, and `undo` cannot make it one.** What a restored row
     carries is what was archived; everything else is gone or defaulted, and the answer
     says so per row (`not_recorded`) instead of leaving the caller to notice a missing
     tag weeks later. Her ruling: an episode's lost metadata is accepted, no migration.

Who owns a row is the second axis, and it is enforced in the query rather than trusted
from the caller: `archived_ids` are matched against `user_id`, so another owner's id is
reported as absent rather than restored under this owner.

Fixtures are synthetic throughout: no summary, key or owner value is copied from a live
base.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from shared.archived_memories import ArchivedMemories
from shared.connection import connection_manager
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

U1 = "u_undo_1"
U2 = "u_undo_2"


@pytest.fixture
async def cm(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """A real migrated database, not a mocked one.

    `undo` is a claim about rows: that a row left one table and arrived in another,
    and that nothing was written on the way. A mock would prove which methods were
    called and none of that.
    """
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.fixture
async def app(cm: Any) -> Any:
    from mcp_server.context import AppContext

    return AppContext()


@pytest.fixture
def ctx(app: Any) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


async def _archive(
    cm: Any,
    *,
    original_id: int | None,
    content: str,
    memory_type: str | None = "episode",
    reason: str = "forget_primitive_fuzzy_l3",
    user_id: str = U1,
    importance: float = 0.5,
) -> int:
    return await ArchivedMemories(cm=cm).archive(
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


async def _counts(cm: Any) -> dict[str, int]:
    """Every table `undo` could touch, so "nothing was written" is checkable at once."""
    out = {}
    for table in ("episodes", "core_memory", "archived_memories"):
        rows = await _rows(cm, f"SELECT COUNT(*) FROM {table}")
        out[table] = int(rows[0][0])
    return out


async def _undo(
    ctx: Any,
    ids: list[int] | None,
    *,
    user_id: str = U1,
    dry_run: bool = False,
    layer: str = "user",
    memory_type: str | None = None,
) -> dict[str, Any]:
    from mcp_server.tools.primitives.forget import forget

    return await forget(
        user_id=user_id,
        scope="undo",
        archived_ids=ids,
        dry_run=dry_run,
        layer=layer,
        memory_type=memory_type,
        ctx=ctx,
    )


async def test_undo_brings_an_episode_back_into_episodes(app: Any, ctx: Any, cm: Any) -> None:
    """The row returns where it came from, and the archive row is consumed."""
    await _archive(cm, original_id=41, content="the episode that was swept", memory_type="episode", importance=0.7)

    result = await _undo(ctx, [41])

    assert result["restored"] == 1, result
    assert result["refused"] == [] and result["not_found"] == []
    episodes = await _rows(cm, "SELECT summary, emotional_weight FROM episodes WHERE user_id=?", (U1,))
    assert len(episodes) == 1
    assert episodes[0][0] == "the episode that was swept"
    assert episodes[0][1] == pytest.approx(0.7)
    assert await _counts(cm) == {"episodes": 1, "core_memory": 0, "archived_memories": 0}, "the archive row was copied, not consumed"


async def test_a_restored_core_row_goes_back_to_core_memory(app: Any, ctx: Any, cm: Any) -> None:
    """An L4 row is split on the first `=` and keeps its kind — not filed as an episode."""
    await _archive(cm, original_id=42, content="favourite_colour=green", memory_type="fact", importance=0.9)

    result = await _undo(ctx, [42])

    assert result["restored"] == 1
    rows = await _rows(cm, "SELECT key, value, memory_kind, importance FROM core_memory WHERE user_id=?", (U1,))
    assert len(rows) == 1
    assert rows[0][0] == "favourite_colour" and rows[0][1] == "green"
    assert rows[0][2] == "fact"
    assert rows[0][3] == pytest.approx(0.9)
    assert await _rows(cm, "SELECT episode_id FROM episodes") == [], "a core row landed in episodes"


async def test_a_row_labelled_with_its_source_table_goes_back_to_core_memory(app: Any, ctx: Any, cm: Any) -> None:
    """`memory_type` names a source when it is a table name, and the route follows it.

    `core_memory` is written by `scripts/purge_foreign_rows.py` for rows it selected from
    `core_memory`, so it means "this came from `core_memory`" — the same convention as the
    singular `episode` naming the table `episodes`.
    """
    await _archive(cm, original_id=43, content="a_key=a_value", memory_type="core_memory", reason="inactive_90d")

    result = await _undo(ctx, [43])

    assert result["restored"] == 1
    rows = await _rows(cm, "SELECT key, value, memory_kind FROM core_memory WHERE user_id=?", (U1,))
    assert rows[0][0] == "a_key" and rows[0][1] == "a_value"
    assert rows[0][2] == "core_memory", "the recorded label is written back exactly, not replaced by a guess"


async def test_undo_reports_what_came_back_and_where_it_went(app: Any, ctx: Any, cm: Any) -> None:
    """Never restore blind: an id and a count cannot say what landed where."""
    await _archive(cm, original_id=44, content="an old episode", memory_type="episode")
    await _archive(cm, original_id=45, content="k=v", memory_type="fact")

    result = await _undo(ctx, [44, 45])

    items = {i["original_id"]: i for i in result["restored_items"]}
    assert set(items) == {44, 45}
    assert items[44]["destination"] == "episodes"
    assert items[45]["destination"] == "core_memory"
    assert items[44]["memory_type"] == "episode"
    assert len(result["restored_items"]) == result["restored"], "the count and the list disagree"


async def test_undo_dry_run_writes_nothing_anywhere(app: Any, ctx: Any, cm: Any) -> None:
    """The central assertion: the plan of a restore leaves all three tables as they were."""
    await _archive(cm, original_id=46, content="preview me", memory_type="episode")
    before = await _counts(cm)

    result = await _undo(ctx, [46], dry_run=True)

    assert result["dry_run"] is True
    assert result["restored"] == 0, "a preview restored something"
    assert [i["original_id"] for i in result["restored_items"]] == [46], "the preview must still name the row"
    assert await _counts(cm) == before
    assert await _rows(cm, "SELECT episode_id FROM episodes") == []


async def test_the_preview_names_what_the_real_call_restores(app: Any, ctx: Any, cm: Any) -> None:
    """One computation, two modes — the reason a preview cannot drift from the pass."""
    await _archive(cm, original_id=47, content="same both times", memory_type="episode")

    preview = await _undo(ctx, [47], dry_run=True)
    real = await _undo(ctx, [47])

    assert [i["original_id"] for i in preview["restored_items"]] == [i["original_id"] for i in real["restored_items"]]
    assert [i["destination"] for i in preview["restored_items"]] == [i["destination"] for i in real["restored_items"]]
    assert real["restored"] == 1 and preview["restored"] == 0


async def test_an_id_that_names_nothing_is_reported_not_guessed(app: Any, ctx: Any, cm: Any) -> None:
    result = await _undo(ctx, [999])

    assert result["restored"] == 0
    assert result["not_found"] == [999]
    assert result["refused"] == []


async def test_another_owners_row_is_not_restored_under_this_owner(app: Any, ctx: Any, cm: Any) -> None:
    """The owner is matched in the query, so a stolen id is absent rather than honoured."""
    await _archive(cm, original_id=48, content="her private note", memory_type="episode", user_id=U2)

    result = await _undo(ctx, [48], user_id=U1)

    assert result["restored"] == 0
    assert result["not_found"] == [48]
    assert await _rows(cm, "SELECT episode_id FROM episodes WHERE user_id=?", (U1,)) == []
    assert await _rows(cm, "SELECT episode_id FROM episodes WHERE user_id=?", (U2,)) == []
    assert len(await _rows(cm, "SELECT id FROM archived_memories WHERE user_id=?", (U2,))) == 1, "her row was consumed by my call"


async def test_a_purged_row_is_refused_with_its_reason(app: Any, ctx: Any, cm: Any) -> None:
    """A deliberate removal is not a forgetting, and `undo` does not turn it into one."""
    await _archive(cm, original_id=49, content="deliberately removed", memory_type="episode", reason="foreign_agent_contamination")

    result = await _undo(ctx, [49])

    assert result["restored"] == 0
    assert [r["reason"] for r in result["refused"]] == ["purged"]
    assert result["not_found"] == [], "a purge is not an absence — that is the silence this refusal ends"
    assert await _rows(cm, "SELECT episode_id FROM episodes") == []
    assert len(await _rows(cm, "SELECT id FROM archived_memories WHERE original_id=?", (49,))) == 1


async def test_a_purged_row_is_refused_when_the_id_is_confirmed(app: Any, ctx: Any, cm: Any) -> None:
    """Naming the reason explicitly must not become a way around the refusal.

    This is the path a caller reaches for when it knows what the row is — the same
    shape as PR #99's "refused again when the kind is named too".
    """
    await _archive(cm, original_id=50, content="deliberately removed", memory_type="episode", reason="foreign_agent_contamination")

    result = await _undo(ctx, [50])

    assert result["restored"] == 0
    assert [r["reason"] for r in result["refused"]] == ["purged"]


async def test_a_row_whose_kind_was_never_recorded_is_refused(app: Any, ctx: Any, cm: Any) -> None:
    """`NULL` means the writer did not say; a `fact` here would be invented provenance."""
    await _archive(cm, original_id=51, content="conflict_9: something", memory_type=None, reason="conflict_resolved")

    result = await _undo(ctx, [51])

    assert result["restored"] == 0
    assert [r["reason"] for r in result["refused"]] == ["unroutable"]
    assert await _rows(cm, "SELECT entry_id FROM core_memory") == []


async def test_a_row_with_no_restore_path_is_refused(app: Any, ctx: Any, cm: Any) -> None:
    """A wiki page body and a graph node have nowhere to go back to in this repository."""
    await _archive(cm, original_id=52, content="a wiki page's body", memory_type="project_archive", reason="project_archived")

    result = await _undo(ctx, [52])

    assert result["restored"] == 0
    assert [r["reason"] for r in result["refused"]] == ["unroutable"]


async def test_a_refusal_is_not_a_missing_row(app: Any, ctx: Any, cm: Any) -> None:
    """Three answers, three lists — collapsing them is how a refusal goes invisible."""
    await _archive(cm, original_id=53, content="deliberately removed", memory_type="episode", reason="foreign_agent_contamination")

    result = await _undo(ctx, [53, 999])

    assert result["restored"] == 0
    assert [r["original_id"] for r in result["refused"]] == [53]
    assert result["not_found"] == [999]


async def test_undo_is_idempotent(app: Any, ctx: Any, cm: Any) -> None:
    """A second undo of the same id reports nothing and raises nothing."""
    await _archive(cm, original_id=54, content="only once", memory_type="episode")

    first = await _undo(ctx, [54])
    second = await _undo(ctx, [54])

    assert first["restored"] == 1
    assert second["restored"] == 0
    assert second["not_found"] == [54]
    assert await _rows(cm, "SELECT episode_id FROM episodes") != []
    assert await _rows(cm, "SELECT id FROM archived_memories") == []


async def test_undo_leaves_the_neighbour_alone(app: Any, ctx: Any, cm: Any) -> None:
    await _archive(cm, original_id=55, content="wanted", memory_type="episode")
    await _archive(cm, original_id=56, content="not asked for", memory_type="episode")

    result = await _undo(ctx, [55])

    assert result["restored"] == 1
    summaries = [r[0] for r in await _rows(cm, "SELECT summary FROM episodes")]
    assert summaries == ["wanted"]
    left = await _rows(cm, "SELECT original_id FROM archived_memories")
    assert [r[0] for r in left] == [56]


async def test_undo_needs_a_non_empty_id_list(app: Any, ctx: Any, cm: Any) -> None:
    """Refused rather than treated as "everything": an empty list is not a wildcard."""
    for ids in (None, []):
        result = await _undo(ctx, ids)
        assert result["error"] == "archived_ids_required", result


async def test_a_restored_episode_does_not_carry_what_the_archive_never_kept(app: Any, ctx: Any, cm: Any) -> None:
    """The limit is asserted and reported, not discovered later.

    Measured 2026-10-11 on the **migrated** schema: `episodes` has `layer` and `memory_kind`
    and `tags` beside the summary, and `core_memory` has 15 columns. The archive stores eight,
    and of an episode it keeps `user_id`, `content`, `memory_type`, `importance` and the
    reason — **no `tags`, no `created_at`, no `memory_kind`, no recorded `layer`**. So a
    restored episode is stamped with the moment it returns and with the layer this call was
    made in, and is tagged with nothing. Her ruling: accepted, no migration.

    The values below are the **measured** shapes, not assumed ones: `EpisodicMemory.save()`
    writes `json.dumps(tags or [])`, so an untagged episode lands as the empty list `'[]'`
    rather than as `NULL`, and `layer` is the third-case proof — the call is made in `agent`
    while the column's own default is `'user'`, so a default could not produce it.
    """
    await _archive(cm, original_id=57, content="an episode with history", memory_type="episode", importance=0.25)
    before = time.time()

    result = await _undo(ctx, [57], layer="agent")

    assert result["restored"] == 1
    row = await _rows(cm, "SELECT tags, created_at, layer, memory_kind FROM episodes WHERE user_id=?", (U1,))
    assert row[0][0] == "[]", "no tags were archived, so a restored episode carries an empty list — not the tags it may once have had"
    assert row[0][1] >= before, "created_at is the moment of return, not the moment it happened"
    assert row[0][2] == "agent", "the layer is the one this call asked for, not the archived one and not the column default"
    assert row[0][3] is None
    assert set(result["restored_items"][0]["not_recorded"]) >= {"tags", "created_at", "memory_kind", "layer"}
    assert "summary" not in result["restored_items"][0]["not_recorded"], "the summary did come back"


async def test_an_ambiguous_id_needs_the_kind_the_listing_showed(app: Any, ctx: Any, cm: Any) -> None:
    """Two rows, one number: refused until the caller says which.

    This closes a loop between the two changes. The listing in PR #99 deliberately **shows**
    an id that names two rows of different kinds rather than hiding it, because the
    `memory_type` it returns is the argument that resolves it. That reasoning only holds if
    the surface can pass the kind back — otherwise the listing advertised a row that nothing
    an agent can call would ever return.
    """
    await _archive(cm, original_id=60, content="the episode", memory_type="episode")
    await _archive(cm, original_id=60, content="k=v", memory_type="fact")

    refused = await _undo(ctx, [60])

    assert refused["restored"] == 0
    assert [r["reason"] for r in refused["refused"]] == ["ambiguous"]
    assert set(refused["refused"][0]["kinds"]) == {"episode", "fact"}, "the refusal must name the kinds that collided"

    resolved = await _undo(ctx, [60], memory_type="episode")

    assert resolved["restored"] == 1, resolved
    assert [r[0] for r in await _rows(cm, "SELECT summary FROM episodes")] == ["the episode"]
    left = await _rows(cm, "SELECT memory_type FROM archived_memories")
    assert [r[0] for r in left] == ["fact"], "the other row was taken instead of the one that was named"


async def test_a_restored_core_row_reports_the_fields_it_did_not_bring(app: Any, ctx: Any, cm: Any) -> None:
    """The same limit on the other route, computed from the schema rather than listed by hand."""
    await _archive(cm, original_id=58, content="a_key=a_value", memory_type="fact")

    result = await _undo(ctx, [58])

    not_recorded = set(result["restored_items"][0]["not_recorded"])
    assert not_recorded >= {"expires_at", "source", "metadata", "layer", "visibility"}
    assert not_recorded.isdisjoint({"key", "value", "importance", "user_id", "memory_kind"}), "these are written from the archive"


async def test_a_broken_row_is_reported_and_does_not_silence_the_call(app: Any, ctx: Any, cm: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The outer handler: a failure is an answer, not a log line.

    The handler is hard to reach from outside — it catches what the row loop was not
    written to expect — so it is reached here by making one row raise. What must hold is
    that the caller learns the call broke: before this change the exception was logged and
    the outcome came back looking like an ordinary partial result, which for a batch of one
    row is indistinguishable from "there was nothing to give back".
    """
    from lifecycle import forgetting as forgetting_module

    await _archive(cm, original_id=95, content="delta = 4", memory_type="fact")

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the write exploded")

    monkeypatch.setattr(forgetting_module.ForgettingSystem, "_restore_core", boom)

    result = await _undo(ctx, [95])

    assert result["restored"] == 0
    assert [r["reason"] for r in result["refused"]] == ["error"], result
    assert result["refused"][0]["original_id"] == 95, "the id in flight is named, not left blank"
    assert "RuntimeError" in result["refused"][0]["detail"]
    left = await _rows(cm, "SELECT original_id FROM archived_memories")
    assert [r[0] for r in left] == [95], "the row was not written, so it must still be in the bin"


async def test_the_preview_refuses_a_key_the_pass_would_refuse(app: Any, ctx: Any, cm: Any) -> None:
    """The drift I found in this file's own promise, on the one input that showed it.

    `dry_run` and the pass run the same loop, so they agree about everything the loop
    decides — provided the loop decides it before the write. A key already in the table
    was decided by the write itself, which `dry_run` skips, so the preview offered to give
    back a row the pass refused. "The preview cannot drift from the pass" was therefore
    true of the rows and false of the refusals, which is the half that matters when the
    answer is going to be acted on.
    """
    await _archive(cm, original_id=90, content="gamma = 3", memory_type="fact")
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, created_at, updated_at, memory_kind) VALUES (?,?,?,?,?,?,?,?)",
        ("user", U1, "gamma ", " 3", 0.5, time.time(), time.time(), "fact"),
    )
    await conn.commit()

    preview = await _undo(ctx, [90], dry_run=True)
    real = await _undo(ctx, [90])

    assert [r["reason"] for r in preview["refused"]] == ["already_present"], preview
    assert [r["reason"] for r in real["refused"]] == [r["reason"] for r in preview["refused"]], "the two modes must refuse the same thing"
    assert preview["restored"] == 0 and real["restored"] == 0
    assert preview["restored_items"] == [] and real["restored_items"] == []
    left = await _rows(cm, "SELECT original_id FROM archived_memories")
    assert [r[0] for r in left] == [90], "a refused row stays in the bin"


async def test_one_bad_core_row_does_not_take_back_the_core_row_before_it(app: Any, ctx: Any, cm: Any) -> None:
    """The reason the L4 route is written inside a savepoint, and this is the case that shows it.

    Two L4 rows in one call, the second one colliding with a key that is already there. The
    first has been written on this connection with no frame of its own, so clearing the
    second with a whole-transaction `rollback()` would take the first back as well — while
    `restored` went on counting it. The count would then describe a row that is not in the
    table, which is the kind of answer this whole repair exists to stop producing.

    An episode-first batch cannot show this: `EpisodicMemory.save()` commits for itself, so
    an episode survives any rollback and hides the defect. L4 against L4 is the arrangement
    that tells the two implementations apart.
    """
    await _archive(cm, original_id=80, content="alpha = 1", memory_type="fact")
    await _archive(cm, original_id=81, content="beta = 2", memory_type="fact")
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, created_at, updated_at, memory_kind) VALUES (?,?,?,?,?,?,?,?)",
        ("user", U1, "beta ", " 2", 0.5, time.time(), time.time(), "fact"),
    )
    await conn.commit()

    result = await _undo(ctx, [80, 81])

    assert result["restored"] == 1, f"one row returned and one was refused: {result}"
    assert [r["reason"] for r in result["refused"]] == ["already_present"], result["refused"]
    present = sorted(r[0] for r in await _rows(cm, "SELECT key FROM core_memory"))
    assert "alpha " in present, "the row that was counted as restored must still be in the table"
    assert present == ["alpha ", "beta "], present


async def test_a_restored_core_row_lands_in_the_layer_that_asked_for_it(app: Any, ctx: Any, cm: Any) -> None:
    """The layer is the call's, on both routes — an episode already does this.

    `episodes` gets it because `EpisodicMemory(cm=..., layer=self.layer)` takes it as an
    argument. `core_memory` is written by a raw `INSERT` that does not name the column at
    all, so it takes the **column default** instead — `'user'` — and a caller working in
    another layer gets their row filed somewhere they were not looking. The `layer` entry
    in `not_recorded` says the archive never kept one; it does not say the column was
    left to a default when the call had said otherwise.
    """
    await _archive(cm, original_id=71, content="theme = dark", memory_type="fact")

    result = await _undo(ctx, [71], layer="agent")

    assert result["restored"] == 1
    row = await _rows(cm, "SELECT layer, key, value FROM core_memory")
    assert row[0][0] == "agent", "the layer of the call that restored it, not the column default"
    assert (row[0][1], row[0][2]) == ("theme ", " dark")


async def test_a_core_row_whose_key_is_already_there_is_answered_not_thrown(app: Any, ctx: Any, cm: Any) -> None:
    """`core_memory` has a UNIQUE index on `(layer, user_id, key)`, and the archive is not a backup.

    A key that was re-written after the row was archived is still in the table, so the raw
    `INSERT` hits the index. That must arrive as an answer — one refusal with a reason, and
    the rest of the batch restored — rather than as an exception that swallows the call.

    The good id is asked for **first** on purpose: a fix that cleared the failure with a
    whole-transaction `rollback()` would pass a test that put it last, while quietly taking
    back the row it had already restored and counted.
    """
    await _archive(cm, original_id=72, content="colour = blue", memory_type="fact")
    await _archive(cm, original_id=73, content="size = large", memory_type="fact")
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, created_at, updated_at, memory_kind) VALUES (?,?,?,?,?,?,?,?)",
        ("user", U1, "colour ", " blue", 0.5, time.time(), time.time(), "fact"),
    )
    await conn.commit()

    result = await _undo(ctx, [73, 72])

    assert result["restored"] == 1, f"the batch must survive one bad row: {result}"
    assert [r["reason"] for r in result["refused"]] == ["already_present"], result["refused"]
    assert result["refused"][0]["original_id"] == 72
    assert result["not_found"] == [], "a key that is already there is not 'not found'"
    keys = sorted(r[0] for r in await _rows(cm, "SELECT key FROM core_memory"))
    assert keys == ["colour ", "size "], "the good row landed and the duplicate was not written twice"
    left = sorted(r[0] for r in await _rows(cm, "SELECT original_id FROM archived_memories"))
    assert left == [72], "the row that was refused stays in the bin; the one that returned does not"
