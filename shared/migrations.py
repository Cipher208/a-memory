"""
DB Migrations — async, unified memory.db
All tables in one file. wiki/graph/audit can be split out later under load.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
import contextlib

logger = logging.getLogger(__name__)

DB_NAME = "memory.db"


class Migration:
    def __init__(self, version: int, name: str, up: Callable):
        self.version = version
        self.name = name
        self.up = up


def _get_migrations() -> list[Migration]:
    migrations = []

    async def v1_init(conn):
        """All tables in a single memory.db."""
        await conn.executescript("""
            -- === L2-L4 Core ===
            CREATE TABLE IF NOT EXISTS core_memory (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                importance REAL DEFAULT 0.5, is_conflict INTEGER DEFAULT 0,
                conflict_group_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_core_user ON core_memory(user_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_core_user_key ON core_memory(user_id, key);

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, summary TEXT,
                state_deltas TEXT, topics TEXT, message_count INTEGER DEFAULT 0,
                started_at REAL NOT NULL, ended_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

            CREATE TABLE IF NOT EXISTS episodes (
                episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, summary TEXT NOT NULL,
                emotional_weight REAL DEFAULT 0.5, tags TEXT, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes(user_id);

            -- === Staging + Archived ===
            CREATE TABLE IF NOT EXISTS staging_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default', session_id TEXT NOT NULL,
                event_id TEXT, content TEXT NOT NULL, importance REAL DEFAULT 0.5,
                metadata TEXT DEFAULT '{}', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS archived_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default', original_id INTEGER,
                content TEXT NOT NULL, memory_type TEXT, importance REAL,
                archive_reason TEXT NOT NULL, archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- === Audit ===
            CREATE TABLE IF NOT EXISTS audit_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, action TEXT NOT NULL, layer TEXT,
                target_id TEXT, details TEXT, timestamp REAL NOT NULL
            );

            -- === Rate Limit ===
            CREATE TABLE IF NOT EXISTS rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, timestamp REAL NOT NULL
            );

            -- === Embeddings ===
            CREATE TABLE IF NOT EXISTS embedding_cache (
                text_hash TEXT PRIMARY KEY, embedding BLOB NOT NULL,
                model_name TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- === RAG ===
            CREATE TABLE IF NOT EXISTS rag_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL, path TEXT, content TEXT NOT NULL,
                sha256_hash TEXT, wiki_type TEXT,
                created_at REAL DEFAULT (strftime('%s','now')),
                updated_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL, embedding BLOB
            );
            CREATE TABLE IF NOT EXISTS rag_relations (
                source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'elaborates',
                weight REAL DEFAULT 0.8,
                PRIMARY KEY (source_id, target_id, relation_type)
            );
            CREATE INDEX IF NOT EXISTS idx_rag_user ON rag_pages(user_id);

            -- === Graph ===
            CREATE TABLE IF NOT EXISTS epi_nodes (
                node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL DEFAULT 'user',
                user_id TEXT NOT NULL, content TEXT NOT NULL,
                node_type TEXT NOT NULL, tags TEXT,
                confidence REAL DEFAULT 0.5, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS epi_edges (
                source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
                relation TEXT NOT NULL, weight REAL DEFAULT 0.8,
                created_at REAL NOT NULL,
                PRIMARY KEY (source_id, target_id, relation)
            );
            CREATE TABLE IF NOT EXISTS temporal_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, event_type TEXT NOT NULL,
                content TEXT NOT NULL, timestamp REAL NOT NULL,
                importance REAL DEFAULT 0.5, metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS temporal_links (
                from_event INTEGER NOT NULL, to_event INTEGER NOT NULL,
                link_type TEXT NOT NULL DEFAULT 'follows',
                strength REAL DEFAULT 0.5,
                PRIMARY KEY (from_event, to_event, link_type)
            );

            -- === Wiki ===
            CREATE TABLE IF NOT EXISTS user_wiki (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, wiki_type TEXT NOT NULL,
                title TEXT NOT NULL, content TEXT NOT NULL,
                tags TEXT, importance REAL DEFAULT 0.5,
                source TEXT DEFAULT 'manual',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_wiki (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, wiki_type TEXT NOT NULL,
                title TEXT NOT NULL, content TEXT NOT NULL,
                tags TEXT, importance REAL DEFAULT 0.5,
                source TEXT DEFAULT 'manual',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );

            -- === FileWiki ===
            CREATE TABLE IF NOT EXISTS wiki_index (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL, wiki_type TEXT NOT NULL,
                title TEXT NOT NULL, file_path TEXT NOT NULL,
                tags TEXT, importance REAL DEFAULT 0.5,
                content TEXT DEFAULT '', content_hash TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_path ON wiki_index(file_path);

            -- === FTS5 indexes ===
            CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
                title, content, wiki_type, content=rag_pages, content_rowid=id
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS user_wiki_fts USING fts5(
                title, content, wiki_type, tags, content=user_wiki, content_rowid=entry_id
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS agent_wiki_fts USING fts5(
                title, content, wiki_type, tags, content=agent_wiki, content_rowid=entry_id
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
                title, content, wiki_type, tags, content=wiki_index, content_rowid=entry_id
            );

            -- === Conflict tracking ===
            CREATE TABLE IF NOT EXISTS memory_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, content TEXT NOT NULL,
                is_conflict INTEGER DEFAULT 0, conflict_group_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- === Migration log ===
            CREATE TABLE IF NOT EXISTS migration_log (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL
            );
        """)

    migrations.append(Migration(1, "init_unified_schema", v1_init))

    async def v2_binary_embeddings(conn):
        """Add binary embeddings column for MIB search."""
        try:
            await conn.execute("ALTER TABLE rag_chunks ADD COLUMN bin_embedding BLOB")
        except sqlite3.OperationalError:
            pass  # Column already exists

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_bin
            ON rag_chunks(page_id, id)
            WHERE bin_embedding IS NOT NULL
        """)

    migrations.append(Migration(2, "binary_embeddings", v2_binary_embeddings))

    async def v3_epi_tags(conn):
        """Add epi_tags table for fast tag lookups."""
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS epi_tags (
                    node_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (node_id, tag)
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_epi_tags_tag ON epi_tags(tag)")
        except sqlite3.OperationalError:
            pass

    migrations.append(Migration(3, "epi_tags", v3_epi_tags))

    async def v4_rag_chunks_index(conn):
        """Add index on rag_chunks(page_id, chunk_index) for JOINs."""
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_page_idx ON rag_chunks(page_id, chunk_index)")
        except sqlite3.OperationalError:
            pass  # Index already exists

    migrations.append(Migration(4, "rag_chunks_index", v4_rag_chunks_index))

    async def v5_typed_memory(conn):
        """Add memory_kind column and memory_kind_registry table."""
        try:
            # Registry table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_kind_registry (
                    kind TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    default_importance REAL NOT NULL,
                    decay_rate REAL NOT NULL,
                    never_archive INTEGER NOT NULL DEFAULT 0,
                    requires_expires_at INTEGER NOT NULL DEFAULT 0,
                    boost_on_keywords TEXT NOT NULL DEFAULT '',
                    description TEXT
                )
            """)
            # Seed 13 types
            await conn.execute("""
                INSERT OR IGNORE INTO memory_kind_registry VALUES
                ('instruction','Instruction',0.9,0.0,1,0,'обязательно,важно,critical,never forget,rule,инструкция','Правило/инструкция, не подлежит забыванию'),
                ('fact','Fact',0.5,0.01,0,0,'факт,fact,имя,возраст,день рождения','Атомарный факт'),
                ('decision','Decision',0.7,0.005,0,0,'решение,decided,chose,decision','Принятое решение'),
                ('goal','Goal',0.8,0.005,0,1,'цель,goal,plan,к концу','Цель с дедлайном'),
                ('preference','Preference',0.7,0.003,0,0,'предпочитаю,prefer,like,нравится,не люблю','Предпочтение'),
                ('commitment','Commitment',0.85,0.0,1,1,'обещаю,обязуюсь,commit,promise,согласен','Обязательство'),
                ('relationship','Relationship',0.6,0.002,0,0,'знаком,друг,коллега,knows,friend','Связь'),
                ('observation','Observation',0.4,0.02,0,0,'видел,заметил,noticed,observed','Наблюдение'),
                ('rule','Rule',0.85,0.0,1,0,'запрещено,нельзя,do not,forbidden,rule','Жёсткое правило'),
                ('todo','Todo',0.6,0.005,0,1,'todo,сделать,do later,remind','Задача с дедлайном'),
                ('question','Open Question',0.5,0.05,0,0,'вопрос,?,уточнить,ask later','Открытый вопрос'),
                ('hypothesis','Hypothesis',0.45,0.03,0,0,'возможно,наверное,probably,hypothesis','Гипотеза'),
                ('context','Context',0.3,0.05,0,0,'контекст,background,context','Фоновый контекст')
            """)
        except sqlite3.OperationalError:
            pass
        # Add memory_kind to core_memory
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE core_memory ADD COLUMN memory_kind TEXT")
        # Add expires_at, source, metadata to core_memory
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE core_memory ADD COLUMN expires_at REAL")
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE core_memory ADD COLUMN source TEXT DEFAULT 'manual'")
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE core_memory ADD COLUMN metadata TEXT")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_kind ON core_memory(user_id, memory_kind)")
        # Add memory_kind to episodes
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE episodes ADD COLUMN memory_kind TEXT")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_kind ON episodes(user_id, memory_kind)")
        # Add memory_kind to rag_chunks (for type boost in search)
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE rag_chunks ADD COLUMN memory_kind TEXT")

    migrations.append(Migration(5, "typed_memory", v5_typed_memory))

    async def v6_core_memory_columns(conn):
        """Add expires_at, source, metadata columns to core_memory."""
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE core_memory ADD COLUMN expires_at REAL")
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE core_memory ADD COLUMN source TEXT DEFAULT 'manual'")
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("ALTER TABLE core_memory ADD COLUMN metadata TEXT")

    migrations.append(Migration(6, "core_memory_columns", v6_core_memory_columns))

    async def v7_drop_float_embeddings(conn):
        """Drop float embedding column to save disk space (keep binary only)."""
        try:
            # Check if we should drop (config-driven via keep_float_blobs)
            await conn.execute("ALTER TABLE rag_chunks DROP COLUMN embedding")
            logger.info("Dropped float embedding column from rag_chunks")
        except sqlite3.OperationalError:
            pass  # Column may already be dropped

    migrations.append(Migration(7, "drop_float_embeddings", v7_drop_float_embeddings))

    async def v8_importance_audit(conn):
        """Create importance_audit table for scheduler logging."""
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS importance_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    chunk_id INTEGER,
                    source TEXT NOT NULL,
                    old_importance REAL,
                    new_importance REAL,
                    signal_breakdown TEXT,
                    reason TEXT,
                    rescored_at REAL NOT NULL
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_importance_audit_user ON importance_audit(user_id, rescored_at DESC)")
        except sqlite3.OperationalError:
            pass

    migrations.append(Migration(8, "importance_audit", v8_importance_audit))

    return migrations


class MigrationManager:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager
        self._migrations = _get_migrations()

    async def get_current_version(self) -> int:
        conn = await self._cm.get(DB_NAME)
        try:
            row = await (await conn.execute("SELECT MAX(version) as v FROM migration_log")).fetchone()
            return row["v"] if row and row["v"] else 0
        except sqlite3.OperationalError:
            return 0

    async def migrate(self) -> dict[str, Any]:
        current = await self.get_current_version()
        applied = []
        for migration in self._migrations:
            if migration.version <= current:
                continue
            logger.info("Applying migration v%d: %s" % (migration.version, migration.name))
            conn = await self._cm.get(DB_NAME)
            await migration.up(conn)
            await conn.execute(
                "INSERT INTO migration_log (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, time.time()),
            )
            await conn.commit()
            applied.append({"version": migration.version, "name": migration.name})
        return {"current_version": current, "applied": applied, "new_version": await self.get_current_version()}

    async def get_pending(self) -> list[dict[str, Any]]:
        current = await self.get_current_version()
        return [{"version": m.version, "name": m.name} for m in self._migrations if m.version > current]


migration_manager = MigrationManager()
