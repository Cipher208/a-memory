"""When a memory HAPPENED, versus when the write ran.

The daemon has always sent the source timestamp in its dispatch payload
(`daemon.py`: `"ts": msg.ts`). Nothing downstream read it: `_new_message` did not
forward it, `auto_save_text` had no parameter for it, so `capture()` fell back to
`time.time()` (`shared/l0.py`, `ts = ts_override or time.time()`), and
`EpisodicMemory.save()` hard-coded `time.time()` as well.

The cost is not cosmetic. Recall answers "when did we decide this", and the
30-day sweeper asks `created_at < now - 30d`, so text dated by its backfill is
both mis-ordered and mis-aged. Measured on a live base before this fix: 1937
agent-layer episodes whose source messages span 15.09..04.10 all read
04.10 09:15..13:42 — the two hours of the replay.

These tests hold the source time at each hop, and each one fails if its hop is
reverted:
  1. `capture` honours `ts_override` (the journal's own record).
  2. `distill_and_route(ts=...)` dates the episodes it writes.
  3. `auto_save_text(ts=...)` writes BOTH the journal row and the episodes with
     the source time — the two halves must agree.
  4. `_new_message` forwards a numeric `ts` from the payload and refuses a
     non-numeric one, because `ts_override or time.time()` accepts anything
     truthy and a JSON string would land in a REAL column as text.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def cm(tmp_path, monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _episode_times(cm: Any, rid: int) -> list[float]:
    """created_at of every episode tagged with this journal row, independently."""
    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT summary, tags, created_at FROM episodes")).fetchall()
    return [float(r["created_at"]) for r in rows if f"raw:{rid}" in (r["tags"] or "")]


async def _journal_ts(cm: Any, rid: int) -> float:
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT ts FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row is not None, f"journal row {rid} should exist"
    return float(row["ts"])


@pytest.mark.asyncio
async def test_the_journal_keeps_the_timestamp_it_is_given(cm: Any) -> None:
    """Hop 1: capture writes the caller's ts, not the clock."""
    from shared.l0 import capture

    source_ts = time.time() - 21 * 86400
    rid = await capture("new_message", "user", "u1", "я решила перейти на PostgreSQL", ts_override=source_ts)
    assert rid is not None
    assert abs(await _journal_ts(cm, rid) - source_ts) < 2.0

    # And it really is not "now": three weeks is far outside any rounding.
    assert time.time() - await _journal_ts(cm, rid) > 20 * 86400


@pytest.mark.asyncio
async def test_distillation_dates_episodes_from_the_source_ts(cm: Any) -> None:
    """Hop 2: an episode built from old text is dated old.

    The text is an OBSERVATION on purpose: `route_kind` sends invariants
    (decision/rule/commitment, decay_rate <= 0.005) to L4 and events to L3, so a
    decision sentence would prove nothing about the episodes table.
    """
    from core import MemoryManager
    from graph.epistemic import EpistemicGraph
    from lifecycle.distiller import distill_and_route

    source_ts = time.time() - 14 * 86400
    mem = MemoryManager(cm=cm).get_layer("user", "u1")
    graph = EpistemicGraph(cm=cm, layer="user")
    route = await distill_and_route(
        mem,
        graph,
        "u1",
        "наблюдение: трафик растёт по пятницам стабильно",
        0.6,
        source_rid=101,
        ts=source_ts,
    )
    assert route["l3_saved"] >= 1, "the distiller must have written episodes to date them"

    times = await _episode_times(cm, 101)
    assert times, "episodes carry the raw:<rid> tag"
    assert all(abs(t - source_ts) < 2.0 for t in times), times
    assert all(time.time() - t > 13 * 86400 for t in times), times


@pytest.mark.asyncio
async def test_the_invariant_layer_is_dated_from_the_source_too(cm: Any) -> None:
    """The other writer: L4 facts, via CoreMemory, whose `now` also served created_at."""
    from core import MemoryManager
    from graph.epistemic import EpistemicGraph
    from lifecycle.distiller import distill_and_route

    source_ts = time.time() - 40 * 86400
    mem = MemoryManager(cm=cm).get_layer("user", "u1")
    graph = EpistemicGraph(cm=cm, layer="user")
    route = await distill_and_route(
        mem,
        graph,
        "u1",
        "я решила перейти на PostgreSQL для проекта",
        0.8,
        source_rid=202,
        ts=source_ts,
    )
    assert route["l4_saved"] >= 1, "a decision sentence is an invariant and belongs in L4"

    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT created_at, updated_at FROM core_memory WHERE user_id='u1' AND layer='user'")).fetchall()
    assert rows, "the fact must be in core_memory"
    for r in rows:
        assert abs(float(r["created_at"]) - source_ts) < 2.0, r["created_at"]
        # updated_at stays "now": the row really was last touched now.
        assert time.time() - float(r["updated_at"]) < 60, r["updated_at"]


@pytest.mark.asyncio
async def test_promotion_dates_the_fact_by_the_episode_it_came_from(cm: Any) -> None:
    """The seventh writer, found live while fixing the other six.

    `Consolidation` promotes L3 episodes into L4. It ordered its query by
    `created_at` but never SELECTED it, and did not pass it on, so a fact
    inherited the promotion run's clock instead of its episode's time. Observed
    on a live base: episodes re-dated to 29.09 promoted into L4 rows stamped
    04.10 16:49.
    """
    from lifecycle.consolidation import ConsolidationEngine

    episode_ts = time.time() - 33 * 86400
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at) VALUES (?,?,?,?,?,?)",
        (
            "user",
            "u1",
            "правило: секреты и токены никогда не коммитить в репозиторий",
            0.9,
            "[]",
            episode_ts,
        ),
    )
    await conn.commit()

    promoted = await ConsolidationEngine(cm=cm, layer="user").consolidate_episodes("u1", min_weight=0.3)
    assert promoted >= 1, "the episode must have been promoted to prove the date"

    rows = await (await conn.execute("SELECT created_at, updated_at FROM core_memory WHERE user_id='u1' AND layer='user'")).fetchall()
    assert rows
    for r in rows:
        assert abs(float(r["created_at"]) - episode_ts) < 2.0, f"the fact must inherit the episode's date {episode_ts}, got {r['created_at']}"
        assert time.time() - float(r["updated_at"]) < 60, "updated_at is still 'when the row was written'"


@pytest.mark.asyncio
async def test_a_replay_dates_history_by_its_source_and_not_by_the_run(cm: Any) -> None:
    """Hops 2+3 through the real production path for old text: replay.

    This is the exact loop that collapsed a live base. Rows captured weeks ago
    are distilled by a replay running today; every episode must carry the
    journal's time, not the replay's. Asserted against the SOURCE time, never
    against `time.time()`, so drift cannot make it pass.
    """
    from features.replay import replay
    from shared.l0 import capture

    source_ts = time.time() - 21 * 86400
    rid = await capture(
        "new_message",
        "user",
        "u1",
        "наблюдение: трафик растёт по пятницам стабильно и это повторяется",
        ts_override=source_ts,
    )
    assert rid is not None

    result = await replay(since_days=60)
    assert result["processed"] >= 1, result

    assert abs(await _journal_ts(cm, rid) - source_ts) < 2.0
    times = await _episode_times(cm, rid)
    assert times, "the replay must have distilled the row into episodes"
    assert all(abs(t - source_ts) < 2.0 for t in times), f"episodes must be dated by the source ({source_ts}), got {times}"


@pytest.mark.asyncio
async def test_the_replay_window_reaches_rows_older_than_a_week(cm: Any) -> None:
    """The mechanism behind three live gaps: `received` rows outside a 7-day window.

    Measured on a live base: 18.09 (5 rows), 23.09 (3), 25.09 (10) all sat in
    `received` with zero episodes, because `replay(since_days=7)` is the default.
    Nothing else consumes `received` — `l0_tiers` states it is never archived or
    truncated — so those rows wait indefinitely for a wider window. The intake
    was fine; the window was too small.
    """
    from features.replay import replay
    from shared.l0 import capture

    old = time.time() - 16 * 86400
    rid = await capture("new_message", "user", "u1", "наблюдение: трафик растёт по пятницам стабильно", ts_override=old)
    assert rid is not None

    # The default window cannot see it — this is the documented reason those days were empty.
    assert (await replay(since_days=7))["processed"] == 0
    # A wide enough window can, which is what the backfill will use.
    assert (await replay(since_days=60))["processed"] == 1


class _NullMem:
    """Enough of MemoryManager for _new_message's dedup lookup to run."""

    def __init__(self) -> None:
        self.user_id = "u1"

    async def get_layer(self, *a: Any, **k: Any) -> Any:
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_ts", "expected"),
    [(time.time() - 5 * 86400, "forwarded"), ("2026-09-18T07:03:00", "refused"), (None, "refused")],
)
async def test_new_message_forwards_a_numeric_ts_and_refuses_anything_else(cm: Any, payload_ts: Any, expected: str, monkeypatch) -> None:
    """Hop 4: the daemon's `ts` reaches capture — if, and only if, it is a number.

    A JSON string is truthy, so `ts_override or time.time()` would pass it
    straight into a REAL column as text; every later age comparison against it
    would then be meaningless rather than merely wrong. The check belongs on the
    way in.
    """
    import hooks.user_hooks as uh

    seen: dict[str, Any] = {}

    async def _spy(mem, graph, user_id, text, **kwargs):
        seen.update(kwargs)
        return {"score": 0.0, "saved_l3": False, "saved_l4": False, "saved_graph": False}

    # `_new_message` imports auto_save_text inside the function body, so the
    # module attribute is what has to be replaced; patching the name on
    # user_hooks would not be seen.
    monkeypatch.setattr("hooks.external.auto_save_text", _spy)

    hooks = uh.UserHooks()
    ctx = {
        "text": "я решила перейти на PostgreSQL для проекта",
        "user_id": "u1",
        "source_msg_id": 777,
        "role": "user",
        "ts": payload_ts,
    }
    # graph must be non-None, or the handler returns before it ever reaches the
    # save and the assertion below would pass for the wrong reason.
    await hooks._new_message(ctx, mem=_NullMem(), graph=object())

    if expected == "forwarded":
        assert isinstance(seen.get("ts"), float), seen
        assert abs(seen["ts"] - payload_ts) < 2.0
    else:
        assert seen.get("ts") is None, f"non-numeric ts must be dropped, got {seen.get('ts')!r}"
