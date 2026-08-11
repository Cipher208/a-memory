import contextlib
import logging

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


async def init_rag_db(cm: AsyncConnectionManager, fts_available: bool) -> None:
    """
    Initialize RAG database schema.
    """
    await cm.execute_script(
        DB_NAME,
        """
        CREATE TABLE IF NOT EXISTS rag_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL,
            path TEXT,
            content TEXT NOT NULL,
            sha256_hash TEXT,
            wiki_type TEXT,
            created_at REAL DEFAULT (strftime('%s','now')),
            updated_at REAL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            bin_embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS rag_relations (
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL DEFAULT 'elaborates',
            weight REAL DEFAULT 0.8,
            PRIMARY KEY (source_id, target_id, relation_type)
        );
        CREATE INDEX IF NOT EXISTS idx_rag_user ON rag_pages(user_id);
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_bin ON rag_chunks(page_id, id) WHERE bin_embedding IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_page_idx ON rag_chunks(page_id, chunk_index);
        """,
    )

    if fts_available:
        with contextlib.suppress(Exception):
            await cm.execute_script(
                DB_NAME,
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(title, content, wiki_type, content=rag_pages, content_rowid=id);

                -- Triggers to keep FTS in sync
                CREATE TRIGGER IF NOT EXISTS rag_pages_ai AFTER INSERT ON rag_pages BEGIN
                  INSERT INTO rag_fts(rowid, title, content, wiki_type) VALUES (new.id, new.title, new.content, new.wiki_type);
                END;
                CREATE TRIGGER IF NOT EXISTS rag_pages_ad AFTER DELETE ON rag_pages BEGIN
                  INSERT INTO rag_fts(rag_fts, rowid, title, content, wiki_type) VALUES('delete', old.id, old.title, old.content, old.wiki_type);
                END;
                CREATE TRIGGER IF NOT EXISTS rag_pages_au AFTER UPDATE ON rag_pages BEGIN
                  INSERT INTO rag_fts(rag_fts, rowid, title, content, wiki_type) VALUES('delete', old.id, old.title, old.content, old.wiki_type);
                  INSERT INTO rag_fts(rowid, title, content, wiki_type) VALUES (new.id, new.title, new.content, new.wiki_type);
                END;
                """,
            )
    else:
        logger.warning("[rag] FTS5 not available, skipping rag_fts virtual table creation.")
