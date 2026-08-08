"""
WikiIndex Layer — handles all SQLite and FTS5 operations for Wiki.
Isolates DB logic from filesystem and business logic.
"""

import json
import logging
import time

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME
from wiki.models import WikiEntry

logger = logging.getLogger(__name__)


class WikiIndex:
    """Encapsulates all database operations for the Wiki index."""

    def __init__(self, connection_manager: AsyncConnectionManager, layer: str):
        self._cm = connection_manager
        self.layer = layer

    async def init_db(self):
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_path ON wiki_index(file_path);
            CREATE INDEX IF NOT EXISTS idx_wiki_layer ON wiki_index(layer);
            CREATE INDEX IF NOT EXISTS idx_wiki_type ON wiki_index(wiki_type);
            CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki_index(updated_at);

            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
                title, content, wiki_type, tags,
                content=wiki_index,
                content_rowid=entry_id
            );
            """,
        )

    async def save(self, entry: WikiEntry, content_hash: str):
        """Atomic insert/update in both wiki_index and wiki_fts."""
        now = time.time()
        tags_json = json.dumps(entry.tags)
        conn = await self._cm.get(DB_NAME)

        # Check for existing entry
        cur = await conn.execute(
            "SELECT entry_id, title, content, wiki_type, tags, content_hash FROM wiki_index WHERE file_path=?",
            (entry.file_path,),
        )
        existing = await cur.fetchone()

        if existing:
            if existing["content_hash"] == content_hash:
                return

            entry_id = existing["entry_id"]

            # FTS update requires OLD values for the 'delete' command in external content tables
            await conn.execute(
                "INSERT INTO wiki_fts(wiki_fts, rowid, title, content, wiki_type, tags) VALUES ('delete', ?, ?, ?, ?, ?)",
                (entry_id, existing["title"], existing["content"], existing["wiki_type"], existing["tags"]),
            )

            await conn.execute(
                """UPDATE wiki_index
                   SET title=?, tags=?, importance=?, content=?, content_hash=?, updated_at=?
                   WHERE entry_id=?""",
                (entry.title, tags_json, entry.importance, entry.content, content_hash, now, entry_id),
            )

            # Then insert NEW values
            await conn.execute(
                "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
                (entry_id, entry.title, entry.content, entry.wiki_type, tags_json),
            )
        else:
            cur = await conn.execute(
                """INSERT INTO wiki_index
                   (layer, wiki_type, title, file_path, tags, importance, content, content_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.layer,
                    entry.wiki_type,
                    entry.title,
                    entry.file_path,
                    tags_json,
                    entry.importance,
                    entry.content,
                    content_hash,
                    now,
                    now,
                ),
            )
            entry_id = cur.lastrowid
            await conn.execute(
                "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
                (entry_id, entry.title, entry.content, entry.wiki_type, tags_json),
            )

        await conn.commit()

    async def get_by_path(self, file_path: str) -> dict | None:
        """Fetch metadata and hash by file path."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM wiki_index WHERE file_path=?",
            (file_path,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def search(self, query: str, limit: int = 10) -> list[dict]:
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

    async def list_by_type(self, wiki_type: str, limit: int = 20) -> list[dict]:
        """List entries of a specific type."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM wiki_index WHERE layer=? AND wiki_type=? ORDER BY updated_at DESC LIMIT ?",
            (self.layer, wiki_type, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_all(self, limit: int = 50) -> list[dict]:
        """List all entries in the current layer."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM wiki_index WHERE layer=? ORDER BY updated_at DESC LIMIT ?",
            (self.layer, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete(self, file_path: str):
        """Remove from both tables."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT entry_id, title, content, wiki_type, tags FROM wiki_index WHERE file_path=?",
            (file_path,),
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
