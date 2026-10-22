"""graph_enrich must run on the write dispatch, not on the shared handle.

`AsyncConnectionManager.get(DB_NAME)` hands out ONE connection per database to
every component in the process. `graph_enrich` used to take that handle and hand
it to every phase, and two `await conn.commit()` calls on it published whatever
a handler had left uncommitted there — the failure class #130 rolled back for the
importance sweep ("a commit() there publishes a handler's uncommitted row").

The dispatch (`shared/write_dispatch.py`) is the process's only owner of
transactions. These tests pin three things:

* a foreign uncommitted row is not published by the pass (the #130 shape);
* the shared handle is never committed by the pass;
* the writes go through the dispatch AND are chunked — one transaction for a
  whole phase would hold the dispatch's process-wide write lock for the whole
  pass (measured 15649 ms for the dream phase) and stall every tool write.
"""

from __future__ import annotations

import contextlib
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
    cannot take the write lock while a handler holds it, and the test below
    holds it deliberately. 15 s of waiting per blocked statement would make
    the suite crawl without testing anything new.
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
    """A connection that reports the SQL it was asked to run."""

    def __init__(self, conn: Any, statements: list[str]) -> None:
        self._conn = conn
        self._statements = statements

    async def execute(self, sql: str, params: Any = ()) -> Any:
        self._statements.append(sql)
        return await self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _DispatchSpy:
    """A dispatch that records what each of its transactions did."""

    def __init__(self, real: WriteDispatch) -> None:
        self._real = real
        self.transactions: list[list[str]] = []
        self.queries: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def query(self, db: str, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return await self._real.query(db, sql, params)

    async def atomic(self, db: str, fn: Any) -> Any:
        statements: list[str] = []

        async def _wrapped(conn: Any) -> Any:
            return await fn(_RecordingConn(conn, statements))

        try:
            return await self._real.atomic(db, _wrapped)
        finally:
            self.transactions.append(statements)


def _durable_probe_rows(base_dir: Any) -> int:
    """How many `foreign_probe` rows a SECOND connection can see. Committed, or nothing."""
    with sqlite3.connect(f"file:{base_dir}/{DB_NAME}?mode=ro", uri=True) as other:
        try:
            return int(other.execute("SELECT COUNT(*) FROM foreign_probe").fetchone()[0])
        except sqlite3.OperationalError:  # the table was itself rolled away
            return 0


async def _seed_heuristic_edges(count: int, *, created_at: float | None = None) -> None:
    """Seed `count` fresh heuristic edges — the NREM pass's raw material."""
    dispatch = get_write_dispatch(connection_manager)
    stamp = created_at if created_at is not None else time.time()

    async def _run(conn: Any) -> None:
        for i in range(count):
            await conn.execute(
                "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
                " VALUES (?, ?, 'mentions', 0.6, ?, '[\"heuristic:cofire\"]')",
                (i + 1, i + 2, stamp),
            )

    await dispatch.atomic(DB_NAME, _run)


async def test_graph_enrich_does_not_publish_a_foreign_uncommitted_row(db, no_miners, monkeypatch):
    """A handler's uncommitted row must stay uncommitted after the pass.

    The shape of #130: a `commit()` on the shared handle publishes whatever
    anybody left pending on it. The pass reads and writes on the dispatch's own
    connection, so it cannot reach the handler's transaction at all.
    """
    conn = await connection_manager.get(DB_NAME)
    await conn.execute("CREATE TABLE IF NOT EXISTS foreign_probe (v TEXT)")
    await conn.commit()
    # A handler's write that has NOT been committed.
    await conn.execute("INSERT INTO foreign_probe VALUES ('somebody else, still in flight')")
    assert conn.in_transaction, "the setup must leave a real pending transaction"

    # The dispatch's own connection cannot take the write lock while the stranger
    # holds it, so the pass may fail with "database is locked" — what it must
    # never do is publish or discard the stranger's row.
    from lifecycle.graph_enrich import graph_enrich

    with contextlib.suppress(sqlite3.OperationalError):
        await graph_enrich(layer="user")

    assert _durable_probe_rows(db.base_dir) == 0, "the pass published a handler's uncommitted row"
    assert conn.in_transaction, "the pass ended a transaction it does not own"


async def test_graph_enrich_never_commits_the_shared_connection(db, no_miners):
    """The shared handle is never committed by the pass.

    Not "not by the phases that own their transaction" — the pass never commits
    it at all: a commit there publishes a handler's pending work, and a rollback
    there discards it.

    The miners are excluded: their own `ensure_*` helpers call
    `cm.execute_script(...)`, which commits the shared handle. That is a
    separate module and a separate defect — see the report.
    """
    from aiosqlite import Connection

    conn = await connection_manager.get(DB_NAME)
    shared = id(conn)
    commits: list[int] = []
    original_commit = Connection.commit

    async def _tracking_commit(self: Any) -> None:
        commits.append(id(self))
        await original_commit(self)

    Connection.commit = _tracking_commit  # type: ignore[method-assign]
    try:
        from lifecycle.graph_enrich import graph_enrich

        await graph_enrich(layer="user")
    finally:
        Connection.commit = original_commit  # type: ignore[method-assign]

    assert shared not in commits, "the pass committed the shared connection"


async def test_graph_enrich_writes_through_the_dispatch_in_chunks(db, no_miners, monkeypatch):
    """The NREM writes go through the dispatch, and they are chunked.

    One transaction for the whole phase would hold the dispatch's process-wide
    write lock for the whole pass and stall every tool write behind it, so a
    multi-batch pass must commit more than once.
    """
    import lifecycle.graph_enrich as ge

    chunk_size = getattr(ge, "_DREAM_CHUNK", 200)
    await _seed_heuristic_edges(chunk_size + 50)

    spy = _DispatchSpy(get_write_dispatch(connection_manager))
    monkeypatch.setattr("lifecycle.graph_enrich.get_write_dispatch", lambda cm=None: spy, raising=False)

    result = await ge.graph_enrich(layer="user")

    nrem_transactions = [tx for tx in spy.transactions if any("UPDATE epi_edges SET weight=?" in sql for sql in tx)]
    assert len(nrem_transactions) >= 2, f"the NREM pass must commit in batches, got {len(nrem_transactions)} transaction(s)"
    assert all(len(tx) <= chunk_size for tx in nrem_transactions), f"a batch exceeded the chunk size: {nrem_transactions}"
    # The work itself still happens: every fresh edge is boosted.
    assert sum(1 for tx in nrem_transactions for sql in tx if "UPDATE epi_edges SET weight=?" in sql) == chunk_size + 50
    assert result["dream"]["nrem_decayed"] == chunk_size + 50, result["dream"]


async def test_graph_enrich_dream_still_decays_prunes_and_boosts(db, no_miners, monkeypatch):
    """The happy path: the dream phases still do their work and report it."""
    import lifecycle.graph_enrich as ge

    dispatch = get_write_dispatch(connection_manager)

    async def _run(conn: Any) -> None:
        await conn.execute(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
            " VALUES (1, 2, 'mentions', 0.05, ?, '[\"heuristic:cofire\"]')",
            (time.time() - 40 * 86400,),
        )
        await conn.execute(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
            " VALUES (3, 4, 'mentions', 0.60, ?, '[\"heuristic:cofire\"]')",
            (time.time() - 40 * 86400,),
        )
        await conn.execute(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
            " VALUES (5, 6, 'mentions', 0.60, ?, '[\"heuristic:cofire\"]')",
            (time.time(),),
        )

    await dispatch.atomic(DB_NAME, _run)

    spy = _DispatchSpy(dispatch)
    monkeypatch.setattr("lifecycle.graph_enrich.get_write_dispatch", lambda cm=None: spy, raising=False)

    result = await ge.graph_enrich(layer="user")
    dream = result["dream"]

    rows = await dispatch.query(DB_NAME, "SELECT source_id, target_id, weight FROM epi_edges")
    weights = {(r["source_id"], r["target_id"]): float(r["weight"]) for r in rows}
    assert (1, 2) not in weights, "the weak stale edge is pruned"
    assert weights[(3, 4)] == pytest.approx(0.59), weights
    assert weights[(5, 6)] == pytest.approx(0.65), weights
    assert dream["nrem_pruned"] == 1, dream
    assert dream["nrem_decayed"] == 2, dream
    # And the work was done on the dispatch, not on the shared handle.
    assert any("DELETE FROM epi_edges" in sql for tx in spy.transactions for sql in tx), spy.transactions
