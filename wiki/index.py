from __future__ import annotations

"""
WikiIndex Layer — handles all SQLite and FTS5 operations for Wiki.
Isolates DB logic from filesystem and business logic.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, TYPE_CHECKING

from shared.constants import DB_NAME

if TYPE_CHECKING:
    from wiki.models import WikiEntry
    from shared.connection import AsyncConnectionManager

logger = logging.getLogger(__name__)

# All WikiIndex instances (user/agent layers) share one SQLite connection;
# multi-statement mutations must be serialized or a concurrent commit()
# can land another task's half-finished transaction.
_mutation_lock = asyncio.Lock()


class WikiIndex:
    """Encapsulates all database operations for the Wiki index."""

    def __init__(self, connection_manager: AsyncConnectionManager, layer: str):
        self._cm = connection_manager
        self.layer = layer

    async def init_db(self) -> None:
        """Initialize tables and indexes."""
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
        async with _mutation_lock:
            conn = await self._cm.get(DB_NAME)
            try:
                existing = await self._get_existing(conn, entry.file_path)

                if existing:
                    if existing["content_hash"] == content_hash:
                        return
                    await self._update_entry_and_fts(conn, entry, existing, content_hash)
                else:
                    await self._insert_entry_and_fts(conn, entry, content_hash, time.time())

                await conn.commit()
            except Exception:
                # Never leave a half-done index+FTS pair for the next
                # unrelated commit() to finalize.
                with contextlib.suppress(Exception):
                    await conn.rollback()
                raise

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
               SET title=?, tags=?, importance=?, content=?, content_hash=?, wiki_type=?, updated_at=?
               WHERE entry_id=?""",
            (entry.title, tags_json, entry.importance, entry.content, content_hash, entry.wiki_type, now, entry_id),
        )

        await conn.execute(
            "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
            (entry_id, entry.title, entry.content, entry.wiki_type, tags_json),
        )

    async def _insert_entry_and_fts(self, conn: Any, entry: WikiEntry, content_hash: str, now: float) -> None:
        tags_json = json.dumps(entry.tags)
        cur = await conn.execute(
            """INSERT INTO wiki_index
               (layer, wiki_type, title, file_path, tags, importance, content, content_hash, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.layer, entry.wiki_type, entry.title, entry.file_path, tags_json, entry.importance, entry.content, content_hash, now, now),
        )
        entry_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
            (entry_id, entry.title, entry.content, entry.wiki_type, tags_json),
        )

    async def get_by_path(self, file_path: str) -> dict[str, Any] | None:
        """Fetch metadata and hash by file path."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM wiki_index WHERE layer=? AND file_path=?",
            (self.layer, file_path),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Optimized FTS5 search JOINing wiki_fts with wiki_index."""
        conn = await self._cm.get(DB_NAME)
        try:
            cur = await conn.execute(
                """SELECT wi.*, fts.rank
                   FROM wiki_fts fts
                   JOIN wiki_index wi ON fts.rowid = wi.entry_id
                   WHERE wiki_fts MATCH ? AND wi.layer = ?
                   ORDER BY fts.rank LIMIT ?""",
                (query, self.layer, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            logger.exception("Search failed for query '%s'", query)
            return []

    async def list_by_type(self, wiki_type: str, limit: int = 20) -> list[dict[str, Any]]:
        """List entries of a specific type."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM wiki_index WHERE layer=? AND wiki_type=? ORDER BY updated_at DESC LIMIT ?",
            (self.layer, wiki_type, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_all(self, limit: int = 50) -> list[dict[str, Any]]:
        """List all entries in the current layer."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM wiki_index WHERE layer=? ORDER BY updated_at DESC LIMIT ?",
            (self.layer, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete(self, file_path: str) -> None:
        """Remove from both tables."""
        async with _mutation_lock:
            conn = await self._cm.get(DB_NAME)
            try:
                cur = await conn.execute(
                    "SELECT entry_id, title, content, wiki_type, tags FROM wiki_index WHERE layer=? AND file_path=?",
                    (self.layer, file_path),
                )
                row = await cur.fetchone()
                if not row:
                    return

                entry_id = row["entry_id"]
                # Delete from FTS first (using the 'delete' special command)
                await conn.execute(
                    "INSERT INTO wiki_fts(wiki_fts, rowid, title, content, wiki_type, tags) VALUES ('delete', ?, ?, ?, ?, ?)",
                    (entry_id, row["title"], row["content"], row["wiki_type"], row["tags"]),
                )
                # Delete from main index
                await conn.execute("DELETE FROM wiki_index WHERE entry_id=?", (entry_id,))
                await conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    await conn.rollback()
                raise

    async def count(self, wiki_type: str | None = None) -> int:
        """Count entries, optionally filtered by type."""
        conn = await self._cm.get(DB_NAME)
        if wiki_type:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM wiki_index WHERE layer=? AND wiki_type=?",
                (self.layer, wiki_type),
            )
        else:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM wiki_index WHERE layer=?",
                (self.layer,),
            )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def add_link(self, from_path: str, to_path: str, link_type: str) -> int:
        """Create a typed link between two page paths. Idempotent (returns the link_id)."""
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT OR IGNORE INTO wiki_links (layer, from_path, to_path, link_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.layer, from_path, to_path, link_type, time.time()),
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT link_id FROM wiki_links WHERE layer=? AND from_path=? AND to_path=? AND link_type=?",
            (self.layer, from_path, to_path, link_type),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_links(self, path: str) -> list[dict[str, Any]]:
        """Return typed links involving `path`, both out (from) and in (to)."""
        conn = await self._cm.get(DB_NAME)
        out_rows = await (
            await conn.execute(
                "SELECT to_path, link_type FROM wiki_links WHERE layer=? AND from_path=?",
                (self.layer, path),
            )
        ).fetchall()
        in_rows = await (
            await conn.execute(
                "SELECT from_path, link_type FROM wiki_links WHERE layer=? AND to_path=?",
                (self.layer, path),
            )
        ).fetchall()
        links = [{"path": r[0], "link_type": r[1], "direction": "out"} for r in out_rows]
        links += [{"path": r[0], "link_type": r[1], "direction": "in"} for r in in_rows]
        return links
