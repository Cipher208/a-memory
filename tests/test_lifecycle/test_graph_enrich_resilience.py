"""graph_enrich must not fail silently.

The dispatch is the process's only owner of transactions, and BOTH hooks that
call `graph_enrich` wrap the call in `contextlib.suppress(Exception)`
(`hooks/user_hooks.py:102`, `hooks/agent_hooks.py:38`). Every silent failure
inside the pass is therefore invisible: the nightly report looks normal while
half the phases never ran.

These tests pin the places where the pass could fail silently:

* a foreign write transaction on the shared handle — the dispatch's own
  connection would block for the full busy timeout and then raise
  `database is locked`, and the hook would hide it;
* `validate_edges` raising — it used to abort the pass before the orphan GC,
  the gap registry, the dream, the segments and the wiki communities;
* the dream phase raising — it ran under a bare `contextlib.suppress(Exception)`,
  so its failure left no trace at all;
* a chunk boundary splitting a node's DELETE+INSERT group, and a counter that
  reports work that was never applied.
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
import time
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager
from shared.write_dispatch import WriteDispatch, get_write_dispatch


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """A migrated base in a tmp dir — the real `~/.mcp-ariel-memory` is never touched.

    The busy timeout is shortened on purpose: the dispatch's own connection
    cannot take the write lock while a handler holds it, and the tests below
    hold it deliberately. 15 s of waiting per blocked statement would make the
    suite crawl without testing anything new.
    """
    monkeypatch.setattr("shared.connection.BUSY_TIMEOUT_MS", 250)
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.fixture
def no_miners(monkeypatch):
    """The miners write too; they are not what these tests measure."""
    monkeypatch.setattr("lifecycle.graph_miners.MINERS", {})


class _RecordingConn:
    """A connection that reports the SQL it was asked to run, with its params."""

    def __init__(self, conn: Any, statements: list[tuple[str, Any]]) -> None:
        self._conn = conn
        self._statements = statements

    async def execute(self, sql: str, params: Any = ()) -> Any:
        self._statements.append((sql, params))
        return await self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _TxSpy:
    """A dispatch that records the statements of every transaction it runs."""

    def __init__(self, real: WriteDispatch) -> None:
        self._real = real
        self.transactions: list[list[tuple[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def atomic(self, db: str, fn: Any) -> Any:
        statements: list[tuple[str, Any]] = []

        async def _wrapped(conn: Any) -> Any:
            return await fn(_RecordingConn(conn, statements))

        try:
            return await self._real.atomic(db, _wrapped)
        finally:
            self.transactions.append(statements)


async def _seed_valence_nodes(count: int) -> None:
    """Seed `count` nodes joined by `supports` edges — the valence phase's raw material.

    `supports` maps to the `supporting` bucket, so every seeded node is
    (re)tagged: the valence phase builds one DELETE+INSERT group per node.
    """
    dispatch = get_write_dispatch(connection_manager)

    async def _run(conn: Any) -> None:
        ids: list[int] = []
        for i in range(count):
            cur = await conn.execute(
                "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
                " VALUES ('user', 'gu', ?, 'fact', '[]', 0.5, ?)",
                (f"valence fact {i}", time.time()),
            )
            ids.append(int(cur.lastrowid or 0))
        for a, b in itertools.pairwise(ids):
            await conn.execute(
                "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, 'supports', 0.8, ?, '[]')",
                (a, b, time.time()),
            )

    await dispatch.atomic(DB_NAME, _run)


async def test_graph_enrich_skips_the_pass_when_a_foreign_transaction_is_open(db, no_miners, caplog):
    """A foreign write txn on the shared handle must not stall the pass.

    The dispatch has its own connection: while a handler holds the write lock
    there, the dispatch's first write blocks for the full busy timeout and then
    raises `database is locked` — and the hook's `contextlib.suppress(Exception)`
    hides it, so the nightly report looks normal although half the phases never
    ran. The pass must skip instead: log a warning and return fast.
    """
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT INTO l0_journal (ts, event, layer, user_id, text, raw_type)"
        " VALUES (?, 'new_message', 'user', 'gu', 'somebody else is mid-write', 'plain')",
        (time.time(),),
    )
    assert conn.in_transaction, "the setup must leave a real pending transaction"

    from lifecycle.graph_enrich import graph_enrich

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="lifecycle.graph_enrich"):
        result = await graph_enrich(layer="user")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"the pass stalled for {elapsed:.3f}s instead of skipping"
    messages = [r.message for r in caplog.records]
    assert any("foreign transaction" in m for m in messages), messages
    assert result["dream"] == {"nrem_decayed": 0, "nrem_pruned": 0, "rem_bridged": 0, "insights": 0}, result["dream"]
    assert result["sanitation"]["valence_tagged"] == 0, result["sanitation"]
    # The stranger's transaction is still theirs: the pass neither published nor discarded it.
    assert conn.in_transaction


async def test_graph_enrich_continues_when_validate_edges_fails(db, no_miners, monkeypatch, caplog):
    """`validate_edges` must not take the rest of the pass down with it.

    It used to run unguarded, while its neighbours (the valence tagging, the
    centrality) logged. A BUSY there killed the pass before the orphan GC, the
    gap registry, the dream, the segments and the wiki communities — and the
    hook's `contextlib.suppress(Exception)` hid the death.
    """
    import lifecycle.graph_sanitation as sanitation

    async def _boom(conn: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sanitation, "validate_edges", _boom)

    seen: list[str] = []

    async def _fake_dream(dispatch: Any, layer: str) -> dict[str, int]:
        seen.append(layer)
        return {"nrem_decayed": 0, "nrem_pruned": 0, "rem_bridged": 0, "insights": 0}

    monkeypatch.setattr("lifecycle.graph_enrich._dream", _fake_dream)

    from lifecycle.graph_enrich import graph_enrich

    with caplog.at_level(logging.WARNING, logger="lifecycle.graph_enrich"):
        result = await graph_enrich(layer="user")

    assert seen == ["user"], "the pass stopped before the dream phase"
    messages = [r.message for r in caplog.records]
    assert any("validate_edges" in m and "database is locked" in m for m in messages), messages
    assert result["sanitation"]["expired"] == 0, result["sanitation"]


async def test_graph_enrich_logs_a_failing_dream_phase(db, no_miners, monkeypatch, caplog):
    """The dream phase carries most of the dispatch writes — its failure must be visible.

    It ran under a bare `contextlib.suppress(Exception)`: a BUSY there (the
    likeliest failure of the whole pass) left no trace whatsoever.
    """
    import lifecycle.graph_enrich as ge

    async def _boom(dispatch: Any, layer: str) -> dict[str, int]:
        raise RuntimeError("dream phase exploded")

    monkeypatch.setattr(ge, "_dream", _boom)

    with caplog.at_level(logging.WARNING, logger="lifecycle.graph_enrich"):
        result = await ge.graph_enrich(layer="user")

    messages = [r.message for r in caplog.records]
    assert any("dream" in m and "dream phase exploded" in m for m in messages), messages
    assert result["dream"] == {"nrem_decayed": 0, "nrem_pruned": 0, "rem_bridged": 0, "insights": 0}, result["dream"]


async def test_valence_tagging_never_splits_a_node_group_across_transactions(db, no_miners, monkeypatch):
    """A node's DELETE and its INSERT must land in ONE transaction.

    The chunker sliced the flat operation list every N operations, so a
    DELETE+INSERT pair could be cut in half: the DELETE would commit and the
    INSERT would fail, leaving the node without its `valence:*` tag — a state
    the old single-transaction code could not produce.
    """
    import lifecycle.graph_enrich as ge

    await _seed_valence_nodes(3)

    monkeypatch.setattr(ge, "_DREAM_CHUNK", 1)

    spy = _TxSpy(get_write_dispatch(connection_manager))
    monkeypatch.setattr(ge, "get_write_dispatch", lambda cm=None: spy, raising=False)

    await ge.graph_enrich(layer="user")

    groups = 0
    for tx in spy.transactions:
        deletes = [params[0] for sql, params in tx if "DELETE FROM epi_tags" in sql]
        inserts = [params[0] for sql, params in tx if "INSERT OR IGNORE INTO epi_tags" in sql]
        if not deletes and not inserts:
            continue
        groups += 1
        assert sorted(deletes) == sorted(inserts), f"a transaction holds half of a DELETE+INSERT group: {tx}"
    assert groups == 3, f"expected three tagged nodes, got {groups}"


async def test_valence_tagged_counts_only_the_work_that_was_applied(db, no_miners, monkeypatch):
    """The counter must report applied work, not planned work.

    The counter was incremented while the operation list was BUILT, and the
    list was applied afterwards: a failure in between left the report claiming
    tags that were never written.
    """
    import lifecycle.graph_enrich as ge

    await _seed_valence_nodes(3)

    monkeypatch.setattr(ge, "_DREAM_CHUNK", 1)

    real_apply = ge._apply_ops
    calls = {"n": 0}

    async def _failing_apply(dispatch: Any, ops: list[Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk is on fire")
        await real_apply(dispatch, ops)

    monkeypatch.setattr(ge, "_apply_ops", _failing_apply)

    result = await ge.graph_enrich(layer="user")

    assert calls["n"] >= 2, "the test must reach the failing batch"
    assert result["sanitation"]["valence_tagged"] == 1, f"the counter reports work that was never applied: {result['sanitation']}"
