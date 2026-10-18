from __future__ import annotations

"""
WikiIndex Layer — handles all SQLite and FTS5 operations for Wiki.
Isolates DB logic from filesystem and business logic.
"""

import contextlib
import json
import logging
import re
import time
from typing import Any, TYPE_CHECKING

from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch, get_write_dispatch

if TYPE_CHECKING:
    from wiki.models import WikiEntry
    from shared.connection import AsyncConnectionManager

_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _fts5_match_expr(query: str) -> str | None:
    """Tokenize arbitrary user text into a safe FTS5 MATCH expression.

    Each word token is double-quoted (inner quotes doubled); tokens stay ANDed
    exactly like FTS5's implicit bare-word behavior. Returns None when the
    query holds no word tokens — caller must return [] without issuing MATCH
    (a bare comma/quote raises 'fts5: syntax error' — live incident 2026-09-13).
    """
    tokens = _FTS_TOKEN_RE.findall(str(query))
    if not tokens:
        return None
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


logger = logging.getLogger(__name__)

# All WikiIndex instances (user/agent layers) share one write dispatcher, whose
# atomic() serialises multi-statement mutations on the process-wide write lock —
# a concurrent commit() can no longer land another task's half-finished transaction.


class WikiIndex:
    """Encapsulates all database operations for the Wiki index."""

    def __init__(self, connection_manager: AsyncConnectionManager, layer: str):
        self._cm = connection_manager
        self.layer = layer

    @property
    def _dispatch(self) -> WriteDispatch:
        """Process-wide write dispatcher; resolved per access so a repointed `_cm` is honoured."""
        return get_write_dispatch(self._cm)

    async def init_db(self) -> None:
        """Initialize tables and indexes.

        DDL stays on the manager helper: `execute_script` is idempotent, and when a
        foreign transaction already holds the shared write lock it rides it instead
        of blocking a second connection until the busy timeout.
        """
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS wiki_index (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL,
                wiki_type TEXT NOT NULL,
                title TEXT NOT NULL,
                file_path TEXT NOT NULL,
                tags TEXT,
                importance REAL DEFAULT 0.5,
                content TEXT DEFAULT '',
                content_hash TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_layer ON wiki_index(layer);
            CREATE INDEX IF NOT EXISTS idx_wiki_type ON wiki_index(wiki_type);
            CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki_index(updated_at);
            CREATE INDEX IF NOT EXISTS idx_wiki_layer_type_time ON wiki_index(layer, wiki_type, updated_at DESC);

            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
                title, content, wiki_type, tags,
                content=wiki_index,
                content_rowid=entry_id
            );

            CREATE TABLE IF NOT EXISTS wiki_links (
                link_id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL,
                from_path TEXT NOT NULL,
                to_path TEXT NOT NULL,
                link_type TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(layer, from_path, to_path, link_type)
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_links_to ON wiki_links(layer, to_path);
            """,
        )
        # A1.2: lifecycle status column (feature-private table — lazy ALTER,
        # no alembic; existing DBs get the column on first use).
        with contextlib.suppress(Exception):
            await self._cm.execute_script(
                DB_NAME,
                "ALTER TABLE wiki_index ADD COLUMN status TEXT NOT NULL DEFAULT 'active';",
            )
        # Layer-scoped path uniqueness. Created separately and guarded:
        # a legacy DB holding the same file_path in both layers would make
        # the new UNIQUE index fail, and init must not die on that.
        try:
            await self._cm.execute_script(
                DB_NAME,
                """
                DROP INDEX IF EXISTS idx_wiki_path;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_layer_path ON wiki_index(layer, file_path);
                """,
            )
        except Exception as e:
            logger.warning("Could not enforce (layer, file_path) uniqueness: %s", e)

    async def save(self, entry: WikiEntry, content_hash: str) -> None:
        """Atomic insert/update in both wiki_index and wiki_fts."""

        async def _save(conn: Any) -> None:
            existing = await self._get_existing(conn, entry.file_path)
            if existing:
                if existing["content_hash"] == content_hash:
                    return
                await self._update_entry_and_fts(conn, entry, existing, content_hash)
            else:
                await self._insert_entry_and_fts(conn, entry, content_hash, time.time())

        # One transaction: index row and FTS shadow row can never diverge, and a
        # failure rolls both back (atomic owns commit/rollback and the write lock).
        await self._dispatch.atomic(DB_NAME, _save)

    async def _get_existing(self, conn: Any, file_path: str) -> dict[str, Any] | None:
        cur = await conn.execute(
            "SELECT entry_id, title, content, wiki_type, tags, content_hash FROM wiki_index WHERE layer=? AND file_path=?",
            (self.layer, file_path),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _update_entry_and_fts(self, conn: Any, entry: WikiEntry, existing: dict[str, Any], content_hash: str) -> None:
        now = time.time()
        entry_id = existing["entry_id"]
        tags_json = json.dumps(entry.tags)

        # FTS update: delete old, insert new
        await conn.execute(
            "INSERT INTO wiki_fts(wiki_fts, rowid, title, content, wiki_type, tags) VALUES ('delete', ?, ?, ?, ?, ?)",
            (entry_id, existing["title"], existing["content"], existing["wiki_type"], existing["tags"]),
        )

        await conn.execute(
            """UPDATE wiki_index
               SET title=?, tags=?, importance=?, content=?, content_hash=?, wiki_type=?, status=?, updated_at=?
               WHERE entry_id=?""",
            (entry.title, tags_json, entry.importance, entry.content, content_hash, entry.wiki_type, entry.status, now, entry_id),
        )

        await conn.execute(
            "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
            (entry_id, entry.title, entry.content, entry.wiki_type, tags_json),
        )

    async def _insert_entry_and_fts(self, conn: Any, entry: WikiEntry, content_hash: str, now: float) -> None:
        tags_json = json.dumps(entry.tags)
        cur = await conn.execute(
            """INSERT INTO wiki_index
               (layer, wiki_type, title, file_path, tags, importance, content, content_hash, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.layer,
                entry.wiki_type,
                entry.title,
                entry.file_path,
                tags_json,
                entry.importance,
                entry.content,
                content_hash,
                entry.status,
                now,
                now,
            ),
        )
        entry_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
            (entry_id, entry.title, entry.content, entry.wiki_type, tags_json),
        )

    async def get_by_path(self, file_path: str) -> dict[str, Any] | None:
        """Fetch metadata and hash by file path."""
        row = await self._dispatch.select_one(
            DB_NAME,
            "SELECT * FROM wiki_index WHERE layer=? AND file_path=?",
            (self.layer, file_path),
        )
        return dict(row) if row else None

    async def search(self, query: str, limit: int = 10, status: str | None = "active") -> list[dict[str, Any]]:
        """Optimized FTS5 search JOINing wiki_fts with wiki_index.

        A1.2: status filter — default 'active' hides stale/archived; None = all.
        """
        try:
            return await self._fts_search(query, limit, status)
        except Exception:
            # FTS5 syntax error (stray , " ( * …): retry once with a safe
            # tokenized expression (live incident 2026-09-13). No logging here
            # — a punctuation query is normal user input, not a server fault.
            safe_expr = _fts5_match_expr(query)
            if safe_expr is None:
                return []
            try:
                return await self._fts_search(safe_expr, limit, status)
            except Exception:
                logger.exception("Search failed for query '%s'", query)
                return []

    async def _fts_search(self, match_expr: str, limit: int, status: str | None) -> list[dict[str, Any]]:
        status_sql = "AND wi.status = ?" if status else ""
        params_list: list[Any] = [match_expr, self.layer]
        if status:
            params_list.append(status)
        params_list.append(limit)
        rows = await self._dispatch.query(
            DB_NAME,
            f"""SELECT wi.*, fts.rank
                FROM wiki_fts fts
                JOIN wiki_index wi ON fts.rowid = wi.entry_id
                WHERE wiki_fts MATCH ? AND wi.layer = ? {status_sql}
                ORDER BY fts.rank LIMIT ?""",
            tuple(params_list),
        )
        return [dict(r) for r in rows]

    async def list_by_type(self, wiki_type: str, limit: int = 20, status: str | None = "active") -> list[dict[str, Any]]:
        """List entries of a specific type (A1.2: default active only)."""
        status_sql = "AND status = ?" if status else ""
        params_list: list[Any] = [self.layer, wiki_type]
        if status:
            params_list.append(status)
        params_list.append(limit)
        rows = await self._dispatch.query(
            DB_NAME,
            f"SELECT * FROM wiki_index WHERE layer=? AND wiki_type=? {status_sql} ORDER BY updated_at DESC LIMIT ?",
            tuple(params_list),
        )
        return [dict(r) for r in rows]

    async def list_all(self, limit: int = 50, status: str | None = "active") -> list[dict[str, Any]]:
        """List all entries in the current layer (A1.2: default active only)."""
        status_sql = "AND status = ?" if status else ""
        params_list: list[Any] = [self.layer]
        if status:
            params_list.append(status)
        params_list.append(limit)
        rows = await self._dispatch.query(
            DB_NAME,
            f"SELECT * FROM wiki_index WHERE layer=? {status_sql} ORDER BY updated_at DESC LIMIT ?",
            tuple(params_list),
        )
        return [dict(r) for r in rows]

    async def delete(self, file_path: str) -> None:
        """Remove from both tables."""

        async def _delete(conn: Any) -> None:
            cur = await conn.execute(
                "SELECT entry_id, title, content, wiki_type, tags FROM wiki_index WHERE layer=? AND file_path=?",
                (self.layer, file_path),
            )
            row = await cur.fetchone()
            await cur.close()
            if not row:
                return
            entry_id = row["entry_id"]
            # Delete from FTS first (using the 'delete' special command)
            await conn.execute(
                "INSERT INTO wiki_fts(wiki_fts, rowid, title, content, wiki_type, tags) VALUES ('delete', ?, ?, ?, ?, ?)",
                (entry_id, row["title"], row["content"], row["wiki_type"], row["tags"]),
            )
            await conn.execute("DELETE FROM wiki_index WHERE entry_id=?", (entry_id,))

        await self._dispatch.atomic(DB_NAME, _delete)

    async def count(self, wiki_type: str | None = None) -> int:
        """Count entries, optionally filtered by type."""
        if wiki_type:
            total = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM wiki_index WHERE layer=? AND wiki_type=?", (self.layer, wiki_type))
        else:
            total = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM wiki_index WHERE layer=?", (self.layer,))
        return int(total or 0)

    async def add_link(self, from_path: str, to_path: str, link_type: str) -> int:
        """Create a typed link between two page paths. Idempotent (returns the link_id)."""
        await self._dispatch.write(
            DB_NAME,
            "INSERT OR IGNORE INTO wiki_links (layer, from_path, to_path, link_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.layer, from_path, to_path, link_type, time.time()),
        )
        link_id = await self._dispatch.scalar(
            DB_NAME,
            "SELECT link_id FROM wiki_links WHERE layer=? AND from_path=? AND to_path=? AND link_type=?",
            (self.layer, from_path, to_path, link_type),
        )
        return int(link_id or 0)

    async def get_links(self, path: str) -> list[dict[str, Any]]:
        """Return typed links involving `path`, both out (from) and in (to)."""
        out_rows = await self._dispatch.query(DB_NAME, "SELECT to_path, link_type FROM wiki_links WHERE layer=? AND from_path=?", (self.layer, path))
        in_rows = await self._dispatch.query(DB_NAME, "SELECT from_path, link_type FROM wiki_links WHERE layer=? AND to_path=?", (self.layer, path))
        links = [{"path": r["to_path"], "link_type": r["link_type"], "direction": "out"} for r in out_rows]
        links += [{"path": r["from_path"], "link_type": r["link_type"], "direction": "in"} for r in in_rows]
        return links
