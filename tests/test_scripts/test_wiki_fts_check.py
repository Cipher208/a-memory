"""`scripts/wiki_fts_check.py` finds and repairs wiki FTS drift.

The failure this guards against is invisible to every ordinary check. Measured on
a live base and on a deliberately corrupted copy: `PRAGMA integrity_check` said
"ok", `SELECT COUNT(*) FROM wiki_fts` returned a plausible number, and FTS5's own
`'integrity-check'` command passed — while `MATCH 'decision'` returned **nothing**
for a row that was still in `wiki_index`. A silently lost posting is worse than an
error: recall stops finding a memory that is still in the database and nothing
reports it.

So these tests never count rows. They search for a row by its own word and require
the row back — and they corrupt a real index first so the check has something to
catch.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from scripts.wiki_fts_check import indexed_rowids, probe, repair

if TYPE_CHECKING:
    from pathlib import Path

_DDL = """
    CREATE TABLE wiki_index (
        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        layer TEXT NOT NULL DEFAULT 'user',
        wiki_type TEXT NOT NULL DEFAULT 'note',
        title TEXT NOT NULL,
        file_path TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '[]',
        importance REAL NOT NULL DEFAULT 0.5,
        content TEXT NOT NULL DEFAULT '',
        content_hash TEXT,
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    );
    CREATE VIRTUAL TABLE wiki_fts USING fts5(
        title, content, wiki_type, tags, content=wiki_index, content_rowid=entry_id
    );
"""

# Reading a matched ghost row is the failure `probe` reports; how SQLite *words*
# it depends on the build, so the tests assert the failure, not the phrase.
# 3.47+ names the missing row (`fts5: missing row 99 from content table
# 'main'.'wiki_index'`); older builds only diagnose the image (measured: 3.50.4
# locally vs 3.46.x on the CI runner, which turned the suite red for 17 runs).
# Do not tighten these to one spelling — the contract is "the read failed".
_GHOST_READ_FAILURES = ("missing row", "database disk image is malformed")


@pytest.fixture()
def healthy(tmp_path: Path) -> Path:
    """A real external-content index, built the supported way."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    for entry_id, title, content in (
        (1, "Decision: MCP stdio migration", "migration decision recorded here"),
        (2, "Lily evolution", "personality evolution notes about lily"),
    ):
        conn.execute("INSERT INTO wiki_index (entry_id, title, content) VALUES (?, ?, ?)", (entry_id, title, content))
        conn.execute(
            "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, 'note', '[]')",
            (entry_id, title, content),
        )
    conn.commit()
    conn.close()
    return db


def _lose_a_posting(db: Path, title: str) -> None:
    """Remove an index row while leaving the content in place — the quiet drift.

    This is the shape an out-of-band writer produces: `wiki_fts` is
    external-content, so `DELETE FROM wiki_fts` does not remove the posting the
    way the FTS5 protocol expects, and nothing throws.
    """
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM wiki_fts WHERE title = ?", (title,))
    conn.commit()
    conn.close()


def test_a_healthy_index_passes(healthy: Path) -> None:
    conn = sqlite3.connect(f"file:{healthy}?mode=ro", uri=True)
    try:
        assert probe(conn) == []
        assert indexed_rowids(conn) == [1, 2]
    finally:
        conn.close()


def test_ordinary_checks_do_not_see_the_drift(healthy: Path) -> None:
    """Establishes why this script exists: everything else says the base is fine.

    If a future SQLite version starts catching this, the test above still holds —
    this one documents the blindness that made the drift live unnoticed.
    """
    _lose_a_posting(healthy, "Decision: MCP stdio migration")

    conn = sqlite3.connect(healthy)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0] == 2
        conn.execute("INSERT INTO wiki_fts(wiki_fts) VALUES('integrity-check')")
        found = conn.execute("SELECT rowid FROM wiki_fts WHERE wiki_fts MATCH ?", ('"decision"',)).fetchall()
        assert found == [], "the posting is gone: searching for it returns nothing, without an error"
        assert conn.execute("SELECT COUNT(*) FROM wiki_index").fetchone()[0] == 2, "the row itself is still there"
    finally:
        conn.close()


def test_a_lost_posting_is_reported_as_drift(healthy: Path) -> None:
    _lose_a_posting(healthy, "Decision: MCP stdio migration")

    conn = sqlite3.connect(f"file:{healthy}?mode=ro", uri=True)
    try:
        problems = probe(conn)
        assert problems, "a row whose posting is gone must be reported"
        table, detail = problems[0]
        assert table == "wiki_fts"
        assert "missing from the index" in detail, detail
        assert "[1]" in detail, detail
        assert indexed_rowids(conn) == [2], "the index no longer holds row 1"
    finally:
        conn.close()


def test_a_posting_without_content_is_reported_as_drift(healthy: Path) -> None:
    """The direction that was live: the index answers for rows the content lacks.

    Reproduced exactly — a posting for a rowid absent from `wiki_index` makes
    reading a matched row raise `fts5: missing row N from content table`, which is
    the error found on real bases. The fixture inserts the posting directly, which
    is what an out-of-band writer leaves behind.

    The ghost deliberately shares a word with a LIVE row ("migration"): the column
    probe only searches words it can read from the content table, so a ghost made
    of entirely unknown words is caught by the rowid comparison alone. On the live
    bases the ghosts shared terms with the surviving rows — that is how the
    corrupt read was reachable at all.
    """
    conn = sqlite3.connect(healthy)
    conn.execute(
        "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (99, 'Ghost migration entry', 'migration ghost body', 'note', '[]')"
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(f"file:{healthy}?mode=ro", uri=True)
    try:
        problems = probe(conn)
        assert any("absent from wiki_index" in d for _t, d in problems), problems
        failed = [d for _t, d in problems if "reading a matched row" in d and "failed:" in d]
        assert failed, problems
        assert any(frag in failed[0].lower() for frag in _GHOST_READ_FAILURES), failed
    finally:
        conn.close()


def test_reading_a_matched_ghost_row_raises_while_the_join_hides_it(healthy: Path) -> None:
    """Why this matters: the app's JOIN returns a quiet empty result instead.

    `wiki/index.py::_fts_search` joins `wiki_index`, so a ghost posting produces no
    row and no error — recall just finds nothing. That is the silent half of the
    drift, and it is worth pinning down which query shape shows it.
    """
    conn = sqlite3.connect(healthy)
    conn.execute("INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (99, 'Ghost only', 'ghost body', 'note', '[]')")
    conn.commit()

    with pytest.raises(sqlite3.DatabaseError) as raised:
        conn.execute("SELECT title FROM wiki_fts WHERE wiki_fts MATCH ?", ('"ghost"',)).fetchall()
    assert any(frag in str(raised.value).lower() for frag in _GHOST_READ_FAILURES), str(raised.value)

    joined = conn.execute(
        "SELECT wi.title FROM wiki_fts fts JOIN wiki_index wi ON fts.rowid = wi.entry_id WHERE wiki_fts MATCH ?",
        ('"ghost"',),
    ).fetchall()
    assert joined == [], "the join quietly returns nothing instead of raising"
    conn.close()


def test_repair_restores_search(healthy: Path) -> None:
    _lose_a_posting(healthy, "Decision: MCP stdio migration")

    conn = sqlite3.connect(healthy)
    try:
        assert probe(conn), "precondition: drift exists"
        assert repair(conn) == ["wiki_fts"]
        assert probe(conn) == [], "after repair every row must find itself again"
        rows = conn.execute("SELECT rowid FROM wiki_fts WHERE wiki_fts MATCH ?", ('"migration"',)).fetchall()
        assert [r[0] for r in rows] == [1], rows
    finally:
        conn.close()


def test_repair_keeps_every_row_findable(healthy: Path) -> None:
    """A rebuild that emptied the index would pass a naive check and break recall."""
    conn = sqlite3.connect(healthy)
    try:
        repair(conn)
        assert set(indexed_rowids(conn)) == {1, 2}
    finally:
        conn.close()


def test_a_deleted_row_leaves_no_phantom_behind(healthy: Path) -> None:
    """Content removed; after repair the index must not answer for it."""
    conn = sqlite3.connect(healthy)
    conn.execute("DELETE FROM wiki_index WHERE entry_id = 2")
    conn.execute("DELETE FROM wiki_fts WHERE title = 'Lily evolution'")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(healthy)
    try:
        repair(conn)
        assert probe(conn) == []
        assert 2 not in indexed_rowids(conn), "a row that no longer exists must not be findable"
    finally:
        conn.close()


def test_a_token_with_a_hyphen_is_not_reported_as_corruption(healthy: Path) -> None:
    """The false positive the first probe produced.

    Unquoted, FTS5 reads `2026-07-08` as column-filter syntax and raises
    `no such column: 07`. That is a query error, not a broken index, so the probe
    quotes its terms — otherwise every dated title cries wolf.
    """
    conn = sqlite3.connect(healthy)
    conn.execute("INSERT INTO wiki_index (entry_id, title, content) VALUES (3, 'dated 2026-07-08 entry', 'the body text')")
    conn.execute("INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (3, 'dated 2026-07-08 entry', 'the body text', 'note', '[]')")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(f"file:{healthy}?mode=ro", uri=True)
    try:
        assert probe(conn) == [], "a hyphenated token is not corruption"
    finally:
        conn.close()


def test_missing_tables_are_not_an_error(tmp_path: Path) -> None:
    """An old base with no wiki tables at all must report nothing, not crash."""
    db = tmp_path / "memory.db"
    sqlite3.connect(db).close()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert probe(conn) == []
        assert indexed_rowids(conn) == []
    finally:
        conn.close()
