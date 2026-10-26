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
import sqlite3
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


async def test_a_row_with_a_verdict_cannot_be_reclosed_through_the_unaccounted_marker(cm: Any) -> None:
    """The retryable close is a STATE, not a permanent key.

    `gated_out`/`unaccounted_exit` is stampable because the write never happened.
    Once a real close has appended a config-pair verdict, the row HAS a verdict:
    the marker is history, not an exemption, and a third close must lose. A
    marker that never expires let every later closer through — verified by
    blind review: three consecutive closes all returned True.
    """
    from shared.l0 import UNACCOUNTED_EXIT, capture, close_row, find_block

    text = "помни: wal включён, автовакуум выключен"
    rid = await capture("new_message", "user", "u1", text)
    assert rid is not None

    assert await close_row(rid, "gated_out", reason=UNACCOUNTED_EXIT) is True
    assert await close_row(rid, "gated_out", reason="importance_gate_bypass") is True

    assert await close_row(rid, "promoted_l4") is False

    row = await _row(cm, rid)
    assert row["status"] == "gated_out"
    decisions = json.loads(row["decisions"] or "[]")
    assert len(decisions) == 2, decisions
    # The same state the duplicate guard reads: the write DID happen, so the
    # row is a duplicate again and the retry must not re-enter it.
    assert await find_block("user", "u1", text) == rid


async def test_close_row_inside_an_outer_atomic_joins_it(cm: Any) -> None:
    """A close called from inside a transaction must join it, not begin a second one.

    `BEGIN IMMEDIATE` raises "cannot start a transaction within a transaction".
    `close_row` guards on `conn.in_transaction` instead of swallowing that error,
    so a nested close runs on the outer connection and the OUTER commit decides
    whether it happened — never a silent no-op.
    """
    from shared.l0 import capture, close_row
    from shared.write_dispatch import get_write_dispatch

    rid = await capture("new_message", "user", "u1", "пятая строка журнала")
    assert rid is not None
    dispatch = get_write_dispatch()

    async def _outer(conn: Any) -> bool:
        # A write first: this is what puts the connection in the state where a
        # nested BEGIN IMMEDIATE raises.
        await conn.execute("UPDATE l0_journal SET event='outer_write' WHERE id=?", (rid,))
        assert conn.in_transaction
        return await close_row(rid, "saved_l3")

    assert await dispatch.atomic("memory.db", _outer) is True

    row = await _row(cm, rid)
    assert row["status"] == "saved_l3"
    assert row["processed_at"] is not None


async def test_a_busy_begin_does_not_lose_a_concurrent_append(cm: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """If BEGIN cannot take the lock, the close must abort — not read outside it.

    `BEGIN IMMEDIATE` failing with SQLITE_BUSY means another writer holds the
    write lock. Swallowing that error lets the read run outside any transaction:
    it sees the row BEFORE the other writer commits, the other writer then
    commits an append, and this closer's UPDATE still matches (the status never
    changed) and overwrites that append — the loss the CAS exists to prevent.

    Everything here is real: a second connection to the same base holds the
    write lock, its append is uncommitted while the read runs, and it commits
    between the read and the update (the only staged part is that timing — it is
    hooked to the read, because an in-process test cannot otherwise order two
    writers without threads).
    """
    import aiosqlite

    from shared.constants import DB_NAME
    from shared.l0 import UNACCOUNTED_EXIT, capture, close_row
    from shared.write_dispatch import get_write_dispatch

    rid = await capture("new_message", "user", "u1", "шестая строка журнала")
    assert rid is not None
    assert await close_row(rid, "gated_out", reason=UNACCOUNTED_EXIT) is True

    dispatch = get_write_dispatch()
    reader_conn = await dispatch._cm.get(DB_NAME)
    await reader_conn.execute("PRAGMA busy_timeout=50")

    other = await aiosqlite.connect(str(cm.base_dir / DB_NAME))
    other.row_factory = aiosqlite.Row
    try:
        await other.execute("PRAGMA busy_timeout=50")
        await other.execute("BEGIN IMMEDIATE")
        await other.execute(
            "UPDATE l0_journal SET decisions=? WHERE id=?",
            (json.dumps([{"gate": "g1", "config_hash": "deadbeefdeadbeef", "ts": 0.0}]), rid),
        )
        assert other.in_transaction

        real_execute = reader_conn.execute

        async def _commit_after_the_read(sql: str, *args: Any, **kwargs: Any) -> Any:
            cur = await real_execute(sql, *args, **kwargs)
            if sql.lstrip().upper().startswith("SELECT STATUS, DECISIONS FROM L0_JOURNAL"):
                await other.commit()  # the other writer lands between read and update
            return cur

        monkeypatch.setattr(reader_conn, "execute", _commit_after_the_read)

        won = await close_row(rid, "gated_out", reason="late_writer")
    finally:
        if other.in_transaction:
            await other.commit()
        await other.close()

    assert won is False, "a close that cannot take the lock must not write"
    decisions = json.loads((await _row(cm, rid))["decisions"] or "[]")
    assert any(d.get("config_hash") == "deadbeefdeadbeef" for d in decisions), decisions


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


async def test_a_busy_begin_does_not_reopen_a_judged_row(cm: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """`_claim`'s BEGIN guard: a lock it cannot take must abort, not read unlocked.

    Two owners are on the same row. The other one has ALREADY decided it — its
    verdict (`gated_out` under the current config) is written but not yet
    committed. `_claim` must not claim it: the re-read has to happen under the
    write lock, or it reads the pre-commit state, sees no verdict, and the CAS
    matches on the unchanged status once the other writer commits — the row is
    then re-distilled and a refusal becomes `promoted_l4`.

    Staged with real locks, as in the `close_row` BUSY test: a second
    connection holds the write lock with the verdict uncommitted, and its commit
    is hooked to `_claim`'s re-read (an in-process test cannot otherwise order
    two writers without threads). The window SELECT is patched to the stale
    snapshot so the loop's fast path cannot be what refuses the row — otherwise
    this test would never reach the guard.

    AND THE PASS MUST NOT ABORT. A BUSY here is one row's problem: `replay()`
    returns normally with that row skipped and a WARNING naming it. Letting it
    propagate would reach the nightly caller's bare `suppress(Exception)` and
    abandon the whole night's replay with no log — the silence class this
    package exists to remove.
    """
    import logging

    import aiosqlite

    from features.replay import config_hash, replay
    from shared.constants import DB_NAME
    from shared.l0 import UNACCOUNTED_EXIT, capture, close_row
    from shared.write_dispatch import get_write_dispatch

    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    # The state a retry may re-enter, so the snapshot carries no config pair.
    assert await close_row(rid, "gated_out", reason=UNACCOUNTED_EXIT) is True

    conn = await cm.get("memory.db")
    snapshot = dict(
        await (await conn.execute("SELECT id, ts, layer, user_id, text, status, decisions FROM l0_journal WHERE id=?", (rid,))).fetchone()
    )
    assert snapshot["status"] == "gated_out"
    assert "config_hash" not in snapshot["decisions"]

    dispatch = get_write_dispatch()
    claim_conn = await dispatch._cm.get(DB_NAME)
    await claim_conn.execute("PRAGMA busy_timeout=50")
    real_execute = claim_conn.execute

    other = await aiosqlite.connect(str(cm.base_dir / DB_NAME))
    other.row_factory = aiosqlite.Row
    try:
        await other.execute("PRAGMA busy_timeout=50")
        # The other owner's verdict: gated_out under the CURRENT config, uncommitted.
        await other.execute("BEGIN IMMEDIATE")
        await other.execute(
            "UPDATE l0_journal SET decisions=? WHERE id=?",
            (json.dumps([{"gate": "g1", "config_hash": config_hash(), "ts": 0.0}]), rid),
        )
        assert other.in_transaction

        real_query = dispatch.query

        async def _stale_query(db: str, sql: str, params: Any = ()) -> list[dict[str, Any]]:
            if "FROM l0_journal" in sql:
                return [dict(snapshot)]
            return await real_query(db, sql, params)

        monkeypatch.setattr(dispatch, "query", _stale_query)

        async def _commit_after_the_recheck(sql: str, *args: Any, **kwargs: Any) -> Any:
            cur = await real_execute(sql, *args, **kwargs)
            if sql.lstrip().upper().startswith("SELECT STATUS, PROCESSED_AT, DECISIONS FROM L0_JOURNAL"):
                await other.commit()  # the verdict lands between the re-read and the claim
            return cur

        monkeypatch.setattr(claim_conn, "execute", _commit_after_the_recheck)

        with caplog.at_level(logging.WARNING, logger="features.replay"):
            res = await replay(since_days=1, respect_gate=False)
    finally:
        monkeypatch.setattr(claim_conn, "execute", real_execute)
        if other.in_transaction:
            await other.commit()
        await other.close()

    assert res["processed"] == 0, res
    assert res["skipped"] >= 1, res
    row = await _row(cm, rid)
    assert row["status"] == "gated_out", "the verdict written under this config must stand"
    assert await _episodes(cm) == 0, "a judged row must not be re-distilled"
    assert any(r.levelno == logging.WARNING and str(rid) in r.getMessage() for r in caplog.records), (
        f"a skipped claim must be visible, not silent: {[r.getMessage() for r in caplog.records]}"
    )


async def test_a_busy_claim_does_not_abort_the_rest_of_the_pass(cm: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """One row whose claim cannot take the lock must not cost the other rows.

    The nightly caller wraps `replay()` in a bare `suppress(Exception)`, so an
    exception escaping the loop abandons every remaining row AND leaves no log.
    The claim is therefore contained per row: the first row is refused (the lock
    is held), the pass moves on, and the second row is processed normally.

    Both rows are staged as `gated_out` with EMPTY decisions: no config pair, so
    the fast path lets them through, and not `received`, so `classify_received`
    does not run and no close contends for the lock ahead of the claims.

    The lock is released when the FIRST claim's `BEGIN IMMEDIATE` fails — the
    only ordering an in-process test can impose without threads. Nothing about
    the loop itself is staged.
    """
    import logging

    import aiosqlite

    from features.replay import replay
    from shared.constants import DB_NAME
    from shared.l0 import capture
    from shared.write_dispatch import get_write_dispatch

    first = await capture("new_message", "user", "u1", PASSING_TEXT + ", первый пункт")
    second = await capture("new_message", "user", "u1", PASSING_TEXT + ", второй пункт")
    assert None not in (first, second)
    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status='gated_out', processed_at=?, decisions='[]' WHERE id IN (?, ?)", (time.time(), first, second))
    await conn.commit()

    dispatch = get_write_dispatch()
    claim_conn = await dispatch._cm.get(DB_NAME)
    await claim_conn.execute("PRAGMA busy_timeout=50")
    real_execute = claim_conn.execute

    other = await aiosqlite.connect(str(cm.base_dir / DB_NAME))
    other.row_factory = aiosqlite.Row
    held = True
    try:
        await other.execute("PRAGMA busy_timeout=50")
        await other.execute("BEGIN IMMEDIATE")
        assert other.in_transaction

        async def _release_on_the_first_failed_begin(sql: str, *args: Any, **kwargs: Any) -> Any:
            nonlocal held
            try:
                return await real_execute(sql, *args, **kwargs)
            except Exception:
                if held and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                    # The first claim is meant to lose this lock; every later one
                    # runs against a free database.
                    held = False
                    await other.rollback()
                    await other.close()
                raise

        monkeypatch.setattr(claim_conn, "execute", _release_on_the_first_failed_begin)

        with caplog.at_level(logging.WARNING, logger="features.replay"):
            res = await replay(since_days=1, respect_gate=False)
    finally:
        monkeypatch.setattr(claim_conn, "execute", real_execute)
        if held:
            if other.in_transaction:
                await other.rollback()
            await other.close()

    assert res["processed"] == 1, res
    assert res["skipped"] >= 1, res
    first_row = await _row(cm, int(first))
    second_row = await _row(cm, int(second))
    assert second_row["status"] in {"saved_l3", "promoted_l4"}, second_row
    assert first_row["status"] == "gated_out", "the refused row stays for the next pass"
    assert any(r.levelno == logging.WARNING and str(first) in r.getMessage() for r in caplog.records), "the skipped claim must be logged"


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


async def test_a_non_busy_operational_error_is_not_swallowed(cm: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a BUSY means "try again later"; the same class carries permanent failures.

    `sqlite3.OperationalError` is raised both by a held write lock and by a
    malformed database image. Skipping the second with a warning would hide a
    broken base behind one log line per row, so the claim's handler re-raises
    anything `shared.sqlite_busy.is_busy` does not recognise.
    """
    from features.replay import replay
    from shared.l0 import UNACCOUNTED_EXIT, capture, close_row
    from shared.write_dispatch import get_write_dispatch

    # A row that REACHES the claim: `gated_out`/unaccounted_exit records no
    # config pair, so the loop's fast path lets it through. A fresh `received`
    # row would be consumed by `classify_received` before any claim runs.
    rid = await capture("new_message", "user", "u1", PASSING_TEXT)
    assert rid is not None
    assert await close_row(rid, "gated_out", reason=UNACCOUNTED_EXIT) is True

    dispatch = get_write_dispatch()
    real_atomic = dispatch.atomic

    async def _broken_atomic(db: str, fn: Any) -> Any:
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(dispatch, "atomic", _broken_atomic)
    with pytest.raises(sqlite3.OperationalError, match="malformed"):
        await replay(since_days=1, respect_gate=False)
    monkeypatch.setattr(dispatch, "atomic", real_atomic)
