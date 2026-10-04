"""Check — and optionally repair — the wiki full-text indexes.

WHY THIS EXISTS

`wiki_fts` (and the older `user_wiki_fts` / `agent_wiki_fts`) are FTS5
**external-content** tables: they store no text of their own, only an index over a
content table (`wiki_fts` → `wiki_index`). That design has two consequences a
plain `DELETE`/`INSERT` gets wrong:

  * `DELETE FROM wiki_fts WHERE title = ...` deletes no content and does not
    remove the posting the way the index expects. The supported form is
    `INSERT INTO wiki_fts(wiki_fts, rowid, ...) VALUES ('delete', ...)`, which is
    what `wiki/index.py` uses. Anything touching the content table out of band —
    a migration, an import, a one-off cleanup by an operator — drifts the index.
  * There are NO synchronisation triggers, so nothing notices.

WHAT THE DRIFT LOOKS LIKE, AND WHY ORDINARY CHECKS MISS IT

Measured on live bases and on a deliberately corrupted copy (2026-10-04):

    PRAGMA integrity_check            -> "ok"
    SELECT COUNT(*) FROM wiki_fts     -> a correct-looking count
    INSERT INTO wiki_fts(...) 'integrity-check'  -> passes
    MATCH 'decision'                  -> 0 rows, while the content row is right there
    MATCH 'Test'                      -> DatabaseError: database disk image is malformed

Three different symptoms, none of them visible to a check that counts rows. A lost
posting is the quiet one: search silently stops finding a memory that is still in
the database, which is worse than an error because nothing reports it.

So this probes the only thing that matters: for every row in the content table,
a distinctive word from that row must find that row back. A row that cannot find
itself is drift, whether the search threw or returned nothing.

USAGE

    MCP_MEMORY_DATA_DIR=<dir> python3 scripts/wiki_fts_check.py            # report
    MCP_MEMORY_DATA_DIR=<dir> python3 scripts/wiki_fts_check.py --repair   # rebuild

Exit code is 1 when drift is found, so this can gate a check.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.connection import connection_manager
from shared.constants import DB_NAME

#: FTS table -> the content table it indexes. Taken from the schema, not guessed:
#: `wiki_fts` covers the current `wiki_index`, while the two legacy tables name
#: their own content tables. Probing a table against the wrong content source
#: would report drift that does not exist.
FTS_SPECS: tuple[tuple[str, str], ...] = (
    ("wiki_fts", "wiki_index"),
    ("user_wiki_fts", "user_wiki"),
    ("agent_wiki_fts", "agent_wiki"),
)

#: Letters only, at least this many. Skips punctuation, digits and the dates that
#: would otherwise be chosen as a row's "distinctive" word.
_WORD_RE = re.compile(r"[^\W\d_]{5,}", re.UNICODE)

#: How many words per row to verify. One would be enough to catch a lost posting;
#: a couple costs nothing and survives a word that the tokenizer splits.
_WORDS_PER_ROW = 2


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def indexed_rowids(conn: sqlite3.Connection, table: str = "wiki_fts") -> list[int]:
    """Exactly the rowids the index holds postings for.

    Read through `fts5vocab`, the only honest source. Every cheaper route lies on
    an external-content table:

      * `SELECT rowid FROM wiki_fts` reads `wiki_index`, so it can only ever show
        content rows — never the rowids the index has lost.
      * `SELECT COUNT(*) FROM wiki_fts` likewise counts content, not postings.
      * `PRAGMA integrity_check` says "ok", and so does FTS5's own
        `INSERT INTO wiki_fts(wiki_fts) VALUES('integrity-check')`. Both were
        verified to pass on a genuinely drifted base.

    `fts5vocab(... 'instance')` reports the postings themselves, which is what
    drift is about. It works on a read-only connection because the virtual table
    lives in `temp`.
    """
    if table not in _table_names(conn):
        return []
    alias = f"vocab_{table}"
    try:
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.{alias} USING fts5vocab(main, '{table}', 'instance')")
        rows = conn.execute(f"SELECT DISTINCT doc FROM temp.{alias}").fetchall()
    except sqlite3.Error:
        return []
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"DROP TABLE IF EXISTS temp.{alias}")
    return sorted(int(r[0]) for r in rows)


def _probe_terms(conn: sqlite3.Connection, content_table: str) -> list[tuple[int, str]]:
    """(rowid, word) pairs: a row must be findable by a word from its own text.

    Words come from the CONTENT table, so a term is guaranteed to be present in
    the data even when the index has lost it — that is what makes a lost posting
    detectable at all.
    """
    if content_table not in _table_names(conn):
        return []
    terms: list[tuple[int, str]] = []
    for entry_id, title, content in conn.execute(f"SELECT entry_id, title, content FROM {content_table}"):
        words = _WORD_RE.findall(f"{title or ''} {content or ''}".lower())
        # Longest first: the most distinctive word, least likely to be shared.
        for word in sorted(set(words), key=len, reverse=True)[:_WORDS_PER_ROW]:
            terms.append((entry_id, word))
    return terms


def probe(conn: sqlite3.Connection, specs: tuple[tuple[str, str], ...] = FTS_SPECS) -> list[tuple[str, str]]:
    """Return (table, detail) for every index that disagrees with its content.

    Both directions count. An index holding postings for rows the content table
    does not have is the drift found live — 222 such rowids on one base, which
    made reading a matched row raise `fts5: missing row N from content table`. An
    index MISSING postings for rows that do exist is the quieter twin: recall
    silently stops finding a memory that is still in the database, with no error
    anywhere. Neither is a lesser problem.
    """
    present = _table_names(conn)
    problems: list[tuple[str, str]] = []
    for table, content_table in specs:
        if table not in present or content_table not in present:
            continue
        content_ids = {int(r[0]) for r in conn.execute(f"SELECT entry_id FROM {content_table}")}
        indexed = set(indexed_rowids(conn, table))
        if content_ids or indexed:
            ghosts = sorted(indexed - content_ids)
            lost = sorted(content_ids - indexed)
            if ghosts:
                problems.append((table, f"{len(ghosts)} posting rowid(s) absent from {content_table}: {ghosts[:8]}"))
            if lost:
                problems.append((table, f"{len(lost)} row(s) of {content_table} missing from the index: {lost[:8]}"))
        # And the failure that throws rather than answering: reading an indexed
        # COLUMN for a ghost rowid raises `fts5: missing row N from content table`.
        # The application's JOIN hides it (no content row → no result), so this is
        # the one place it surfaces at all.
        for _entry_id, word in _probe_terms(conn, content_table)[:50]:
            try:
                conn.execute(f"SELECT title FROM {table} WHERE {table} MATCH ?", (f'"{word}"',)).fetchall()
            except sqlite3.Error as exc:
                problems.append((table, f"reading a matched row for {word!r} failed: {exc}"))
                break
    return problems


def repair(conn: sqlite3.Connection, specs: tuple[tuple[str, str], ...] = FTS_SPECS) -> list[str]:
    """Rebuild each FTS table from its content table. Returns what was rebuilt."""
    present = _table_names(conn)
    done: list[str] = []
    for table, content_table in specs:
        if table not in present or content_table not in present:
            continue
        conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        done.append(table)
    conn.commit()
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="rebuild the index instead of only reporting")
    args = parser.parse_args()

    db = Path(connection_manager.base_dir) / DB_NAME
    if not db.is_file():
        print(f"no database at {db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{db}?mode={'rw' if args.repair else 'ro'}", uri=True, timeout=10.0)
    try:
        problems = probe(conn)
        checked = sum(len(_probe_terms(conn, c)) for t, c in FTS_SPECS if t in _table_names(conn))
        print(f"{db}")
        print(f"  terms verified against the content tables: {checked}")
        if not problems:
            print("  search: OK")
            return 0
        for table, detail in problems:
            print(f"  DRIFT in {table}: {detail}")
        if not args.repair:
            print("  re-run with --repair to rebuild from the content tables")
            return 1

        stamp = time.strftime("%Y%m%dT%H%M%S")
        backup = db.with_suffix(db.suffix + f".pre-ftsrebuild-{stamp}.bak")
        with sqlite3.connect(str(backup)) as dst, sqlite3.connect(str(db)) as src:
            src.backup(dst)
        print(f"  backup: {backup}")

        done = repair(conn)
        left = probe(conn)
        print(f"  rebuilt: {', '.join(done) or '(nothing)'}")
        print(f"  search after repair: {left or 'OK'}")
        return 0 if not left else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
