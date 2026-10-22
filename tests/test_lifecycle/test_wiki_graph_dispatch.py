"""wiki_graph_builder must run on the write dispatch, not on the shared handle.

`AsyncConnectionManager.get(DB_NAME)` hands out ONE connection per database to
every component in the process. `build_from_wiki` used to take that handle and
end with `await conn.commit()`, which published whatever a handler had left
uncommitted there — the failure class #130 rolled back for the importance sweep
and #142 for graph_enrich ("a commit() there publishes a handler's uncommitted
row"). It also held one transaction open on that shared handle for the whole
build, which is a long write lock by construction.

These tests pin three things:

* a foreign uncommitted row is not published by the build (the #130 shape);
* the shared handle is never committed by the build;
* the writes go through the dispatch AND still land the pages and the links.

Precedent: tests/test_lifecycle/test_graph_enrich_dispatch.py.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager


@pytest.fixture
async def wiki_db(tmp_path, monkeypatch):
    """A migrated base plus a real wiki in a tmp dir; the live base is never touched.

    The busy timeout is shortened on purpose: the dispatch's own connection
    cannot take the write lock while a handler holds it, and one test below
    holds it deliberately. 15 s of waiting per blocked statement would make the
    suite crawl without testing anything new.
    """
    monkeypatch.setattr("shared.connection.BUSY_TIMEOUT_MS", 250)
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    from wiki.manager import WikiManager

    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"))
    await wm.init_db()
    await wm.add(wiki_type="work_notes", title="alpha page", content="Alpha body")
    await wm.add(wiki_type="work_notes", title="beta page", content="Beta body follows [[alpha_page]]")
    yield wm, connection_manager
    connection_manager._conns.clear()


def _durable_probe_rows(base_dir: Any) -> int:
    """How many `foreign_probe` rows a SECOND connection can see. Committed, or nothing."""
    with sqlite3.connect(f"file:{base_dir}/{DB_NAME}?mode=ro", uri=True) as other:
        try:
            return int(other.execute("SELECT COUNT(*) FROM foreign_probe").fetchone()[0])
        except sqlite3.OperationalError:  # the table was itself rolled away
            return 0


async def test_wiki_build_does_not_publish_a_foreign_uncommitted_row(wiki_db):
    """A handler's uncommitted row must stay uncommitted after the build.

    The shape of #130: a `commit()` on the shared handle publishes whatever
    anybody left pending on it. The build reads and writes on the dispatch's
    own connection, so it cannot reach the handler's transaction at all.
    """
    _wm, cm = wiki_db
    conn = await cm.get(DB_NAME)
    await conn.execute("CREATE TABLE IF NOT EXISTS foreign_probe (v TEXT)")
    await conn.commit()
    # A handler's write that has NOT been committed.
    await conn.execute("INSERT INTO foreign_probe VALUES ('somebody else, still in flight')")
    assert conn.in_transaction, "the setup must leave a real pending transaction"

    # The dispatch's own connection cannot take the write lock while the stranger
    # holds it, so the build may fail with "database is locked" — what it must
    # never do is publish or discard the stranger's row.
    from lifecycle.wiki_graph_builder import build_from_wiki

    with contextlib.suppress(sqlite3.OperationalError):
        await build_from_wiki(user_id="default", layer="user")

    assert _durable_probe_rows(cm.base_dir) == 0, "the build published a handler's uncommitted row"
    assert conn.in_transaction, "the build ended a transaction it does not own"


async def test_wiki_build_never_commits_the_shared_connection(wiki_db):
    """The shared handle is never committed by the build.

    Not "not by the phases that own their transaction" — the build never
    commits it at all: a commit there publishes a handler's pending work, and
    a rollback there discards it.
    """
    from aiosqlite import Connection

    _wm, cm = wiki_db
    conn = await cm.get(DB_NAME)
    shared = id(conn)
    commits: list[int] = []
    original_commit = Connection.commit

    async def _tracking_commit(self: Any) -> None:
        commits.append(id(self))
        await original_commit(self)

    Connection.commit = _tracking_commit  # type: ignore[method-assign]
    try:
        from lifecycle.wiki_graph_builder import build_from_wiki

        result = await build_from_wiki(user_id="default", layer="user")
    finally:
        Connection.commit = original_commit  # type: ignore[method-assign]

    assert shared not in commits, "the build committed the shared connection"
    assert result["pages"] >= 2, result  # the build did its work, not a no-op


class _DispatchSpy:
    """A dispatch that records which of its write paths the build used."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.atomics: list[str] = []
        self.writes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def atomic(self, db: str, fn: Any) -> Any:
        self.atomics.append(db)
        return await self._real.atomic(db, fn)

    async def write(self, db: str, sql: str, params: Any = ()) -> int:
        self.writes.append(sql)
        return await self._real.write(db, sql, params)


async def test_wiki_build_writes_through_the_dispatch_and_still_lands(wiki_db, monkeypatch):
    """The nodes and the edges go through the dispatch, and the graph is still built.

    The dispatch is the process's only owner of transactions: a raw handle here
    would hold a write lock for the whole build and end in the shared commit
    this file exists to forbid. The happy path is pinned so the port cannot
    silently stop building anything.
    """
    from shared.write_dispatch import get_write_dispatch

    _wm, cm = wiki_db
    spy = _DispatchSpy(get_write_dispatch(cm))
    monkeypatch.setattr("lifecycle.wiki_graph_builder.get_write_dispatch", lambda cm=None: spy, raising=False)

    from lifecycle.wiki_graph_builder import build_from_wiki

    result = await build_from_wiki(user_id="default", layer="user")

    assert result["pages"] >= 2, result
    assert result["links"] >= 1, result  # the [[alpha_page]] link must land
    assert spy.atomics, "no node write went through the dispatch"
    assert any("INSERT OR IGNORE INTO epi_edges" in sql for sql in spy.writes), spy.writes

    rows = await spy.query(DB_NAME, "SELECT COUNT(*) AS n FROM epi_nodes WHERE node_type='wiki_page'")
    assert rows[0]["n"] >= 2, rows
