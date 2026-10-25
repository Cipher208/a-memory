"""F3+F4+F5 — one verdict per row, one stable key.

A row's terminal status is written once, by the owner of that transition, from
the status it expects (`close_row` is a compare-and-set). Replay never
re-decides a row that already has a verdict (`_claim` refuses the terminal
statuses). The idempotency key is a pure function of configuration, not of
runtime state (`config_hash` stops hashing the EMA).

Each test drives the entry point the invariant lives on: `close_row` and
`replay`, never a helper fed with prepared input.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager

#: The statuses that mean the content was WRITTEN (or deliberately routed), so
#: re-distilling it would duplicate memory. `gated_out` is not here: a refusal is
#: a verdict only under the configuration that made it — see the two
#: `gated_out` tests below.
WRITTEN = ("promoted_l4", "saved_l3", "routed_direct", "foreign_agent")

#: Text the distiller actually turns into memory, so "processed" is observable.
PASSING_TEXT = "я решила перейти на PostgreSQL для проекта X, потому что MySQL не держит нагрузку"


@pytest.fixture
async def cm(tmp_path, monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _age(cm: Any, *rids: int, seconds: float = 1200.0) -> None:
    """Move rows past the 600 s classify grace, as the nightly pass sees them."""
    conn = await cm.get("memory.db")
    placeholders = ",".join("?" for _ in rids)
    await conn.execute(f"UPDATE l0_journal SET ts = ts - ? WHERE id IN ({placeholders})", (seconds, *rids))
    await conn.commit()  # WriteDispatch holds its own connection: uncommitted rows are invisible to it


async def _row(cm: Any, rid: int) -> dict[str, Any]:
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT id, status, processed_at, decisions FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row is not None
    return dict(row)


async def _episodes(cm: Any) -> int:
    conn = await cm.get("memory.db")
    return int((await (await conn.execute("SELECT COUNT(*) FROM episodes")).fetchone())[0])


# --------------------------------------------------------------------------
# R1 — close_row is a compare-and-set
# --------------------------------------------------------------------------


async def test_close_row_does_not_overwrite_a_terminal_status(cm: Any) -> None:
    """A late writer must not turn a saved row into a refusal."""
    from shared.l0 import capture, close_row

    rid = await capture("new_message", "user", "u1", "первая строка журнала")
    assert rid is not None
    await close_row(rid, "saved_l3")

    assert await close_row(rid, "gated_out") is False

    row = await _row(cm, rid)
    assert row["status"] == "saved_l3"
    assert row["processed_at"] is not None


async def test_close_row_returns_true_for_the_first_close(cm: Any) -> None:
    """The owner of the transition is told that it won."""
    from shared.l0 import capture, close_row

    rid = await capture("new_message", "user", "u1", "вторая строка журнала")
    assert rid is not None
    assert await close_row(rid, "saved_l3") is True


async def test_two_closers_only_one_wins(cm: Any) -> None:
    """Two closers expecting the same status: exactly one writes, the other is told so."""
    from shared.l0 import capture, close_row

    rid = await capture("new_message", "user", "u1", "третья строка журнала")
    assert rid is not None

    results = [await close_row(rid, "saved_l3"), await close_row(rid, "gated_out")]

    assert results.count(True) == 1, results
    assert results == [True, False], results
    assert (await _row(cm, rid))["status"] == "saved_l3"


async def test_close_row_does_not_append_decisions_when_it_loses(cm: Any) -> None:
    """A losing close must not leave a decision behind: the row belongs to another writer."""
    from shared.l0 import capture, close_row

    rid = await capture("new_message", "user", "u1", "четвёртая строка журнала")
    assert rid is not None

    assert await close_row(rid, "gated_out", reason="importance_gate_bypass") is True
    assert await close_row(rid, "saved_l3", reason="late_writer") is False

    decisions = json.loads((await _row(cm, rid))["decisions"] or "[]")
    assert len(decisions) == 1, decisions
    assert decisions[0]["reason"] == "importance_gate_bypass"


# --------------------------------------------------------------------------
# R2 — replay never re-decides a row that already has a verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", WRITTEN)
async def test_claim_refuses_terminal_statuses(cm: Any, terminal: str) -> None:
    """Naming a written row by id must not re-open it.

    `replay(ids=...)` ignores the window's status filter, so it is the only
    path that can reach `_claim` with a written row — the defect F4 describes.

    `gated_out` is deliberately NOT in this list. It is not unconditionally
    terminal: a refusal is a verdict only under the configuration that made it,
    so a `gated_out` row is refused when its decisions carry the CURRENT
    (gate, config_hash) pair and claimed otherwise. That rule has its own two
    tests below; including it here would assert the very bug F5 describes.
    """
    from features.replay import replay
    from shared.l0 import capture, close_row

    rid = await capture("new_message", "user", "u1", f"закрытая строка {terminal}")
    assert rid is not None
    # reason=None keeps `decisions` empty, so the config-hash skip cannot be
    # what refuses the row: only the status can decide it.
    await close_row(rid, terminal)

    res = await replay(since_days=1, ids=[rid], respect_gate=False)

    assert res["processed"] == 0, res
    assert res["skipped"] == 1, res
    assert (await _row(cm, rid))["status"] == terminal


async def test_claim_takes_a_gated_out_row_after_the_config_changed(cm: Any) -> None:
    """A row refused under a CHANGED config must be reconsidered.

    That is the whole point of `config_hash`: a fixed distiller (or a corrected
    threshold) has to be able to re-judge what the old one refused. The row
    below was refused under `deadbeefdeadbeef`, which is not the current key,
    so it still awaits a verdict and the window run must process it.
    """
    from features.replay import config_hash, replay
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    conn = await cm.get("memory.db")
    await conn.execute(
        "UPDATE l0_journal SET status='gated_out', processed_at=?, decisions=? WHERE id=?",
        (time.time(), json.dumps([{"gate": "g1", "config_hash": "deadbeefdeadbeef", "ts": 0.0}]), rid),
    )
    await conn.commit()
    assert config_hash() != "deadbeefdeadbeef"

    res = await replay(since_days=1, respect_gate=False)

    assert res["processed"] == 1, res
    assert res["skipped"] == 0, res
    assert (await _row(cm, rid))["status"] in {"saved_l3", "promoted_l4"}


async def test_claim_refuses_a_gated_out_row_judged_by_the_current_config(cm: Any) -> None:
    """The same configuration already refused this row: re-judging it is a loop.

    Without this the nightly pass would re-distil every refusal under an
    unchanged config — the config-hash skip exists to prevent exactly that.
    """
    from features.replay import config_hash, replay
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    conn = await cm.get("memory.db")
    await conn.execute(
        "UPDATE l0_journal SET status='gated_out', processed_at=?, decisions=? WHERE id=?",
        (time.time(), json.dumps([{"gate": "g1", "config_hash": config_hash(), "ts": 0.0}]), rid),
    )
    await conn.commit()

    res = await replay(since_days=1, respect_gate=False)

    assert res["processed"] == 0, res
    assert res["skipped"] == 1, res
    assert (await _row(cm, rid))["status"] == "gated_out"


async def test_claim_refuses_a_row_closed_between_the_snapshot_and_the_claim(cm: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """F4's core: the snapshot is stale, so only `_claim`'s re-read can refuse.

    The window SELECT runs BEFORE any transaction; the claim re-reads the row
    inside `BEGIN IMMEDIATE`. A row can be closed by another owner between the
    two — that is the race F4 describes, and it is deterministic here: the
    dispatch's `query` is patched to return the snapshot as it was taken, while
    the real row has moved on.

    The snapshot is taken while the row is `gated_out`/`unaccounted_exit` — the
    retryable close, which records no config pair and therefore defeats the
    loop's fast-path `_judged_by` check. A concurrent owner (the live retry)
    then closes it with a verdict under the CURRENT config. Only the re-read
    inside `_claim` can see that verdict.

    A `received` snapshot cannot be used to stage this: the loop calls
    `classify_received` for `received` rows, and that re-reads the real row
    (now `gated_out`) and returns None — the row is skipped before `_claim`
    runs, so the test would pass with the refusal branch deleted. Verified by
    mutation, not assumed.
    """
    from features.replay import replay
    from shared.l0 import UNACCOUNTED_EXIT, capture, close_row
    from shared.write_dispatch import get_write_dispatch

    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    # The row a retry may re-enter: the write never happened, no config pair.
    assert await close_row(rid, "gated_out", reason=UNACCOUNTED_EXIT) is True

    conn = await cm.get("memory.db")
    snapshot = dict(
        await (await conn.execute("SELECT id, ts, layer, user_id, text, status, decisions FROM l0_journal WHERE id=?", (rid,))).fetchone()
    )
    assert snapshot["status"] == "gated_out"
    assert "config_hash" not in snapshot["decisions"]

    # ── the concurrent owner finishes the row before the claim runs
    assert await close_row(rid, "gated_out", reason="importance_gate_bypass") is True
    settled = await _row(cm, rid)
    assert settled["decisions"] != snapshot["decisions"]
    assert "config_hash" in settled["decisions"]

    dispatch = get_write_dispatch()
    real_query = dispatch.query

    async def _stale_query(db: str, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        """Hand replay the snapshot it would have read before the concurrent close."""
        if "FROM l0_journal" in sql:
            return [dict(snapshot)]
        return await real_query(db, sql, params)

    monkeypatch.setattr(dispatch, "query", _stale_query)

    res = await replay(since_days=1, respect_gate=False)

    assert res["processed"] == 0, res
    assert res["skipped"] == 1, res
    row = await _row(cm, rid)
    assert row["status"] == "gated_out", "the concurrent verdict must stand"
    assert row["decisions"] == settled["decisions"], "its decisions must be untouched"
    assert await _episodes(cm) == 0, "a row closed under this config must not be re-distilled"


async def test_claim_takes_a_received_row(cm: Any) -> None:
    """The normal nightly path still works: an aged received row is processed."""
    from features.replay import replay
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    await _age(cm, rid)

    res = await replay(since_days=1, ids=[rid], respect_gate=False)

    assert res["processed"] == 1, res
    assert (await _row(cm, rid))["status"] in {"saved_l3", "promoted_l4"}


async def test_claim_takes_a_stale_processing_row(cm: Any) -> None:
    """An abandoned claim (past the 600 s grace) must stay claimable."""
    from features.replay import replay
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status='processing', processed_at=? WHERE id=?", (time.time() - 700.0, rid))
    await conn.commit()

    res = await replay(since_days=1, ids=[rid], respect_gate=False)

    assert res["processed"] == 1, res
    assert (await _row(cm, rid))["status"] in {"saved_l3", "promoted_l4"}


async def test_claim_refuses_a_fresh_processing_row(cm: Any) -> None:
    """An in-flight claim is not stolen."""
    from features.replay import replay
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status='processing', processed_at=? WHERE id=?", (time.time(), rid))
    await conn.commit()

    res = await replay(since_days=1, ids=[rid], respect_gate=False)

    assert res["processed"] == 0, res
    assert res["skipped"] == 1, res
    assert (await _row(cm, rid))["status"] == "processing"


async def test_replay_does_not_redistill_a_terminal_row(cm: Any) -> None:
    """A closed row is never distilled twice — that is how raw:164 became 120 episodes.

    Driven through `ids`: the window SELECT already excludes terminal statuses,
    so only the id override reaches the claim, which is exactly the path F4
    describes. A plain window run cannot see this defect at all.
    """
    from features.replay import replay
    from shared.l0 import capture, close_row

    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    await close_row(rid, "saved_l3")

    res = await replay(since_days=1, ids=[rid], respect_gate=False)

    assert res["processed"] == 0, res
    assert (await _row(cm, rid))["status"] == "saved_l3"
    assert await _episodes(cm) == 0, "a terminal row must not create an episode"


# --------------------------------------------------------------------------
# R3 — the idempotency key is a pure function of configuration
# --------------------------------------------------------------------------


async def test_config_hash_is_stable_across_a_gate_call(cm: Any) -> None:
    """`gate()` trains the EMA on every message; the key must not move with it."""
    from features.replay import config_hash
    from shared.adaptive import adaptive_threshold

    before = config_hash()
    await adaptive_threshold.gate(0.9)
    after = config_hash()

    assert after == before, "runtime state (the EMA) must not enter the idempotency key"


async def test_config_hash_changes_with_the_static_threshold(cm: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key must still track what an operator configured."""
    from config import config
    from features.replay import config_hash

    before = config_hash()
    hooks = dict(config._data.get("hooks") or {})
    hooks["auto_save_threshold"] = float(hooks.get("auto_save_threshold", 0.5)) + 0.17
    monkeypatch.setitem(config._data, "hooks", hooks)
    after = config_hash()

    assert after != before
