"""init_v8_schema

Revision ID: a38d67fcd99e
Revises:
Create Date: 2026-08-07 23:05:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a38d67fcd99e"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Core tables
    op.execute("""
        CREATE TABLE IF NOT EXISTS core_memory (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            importance REAL DEFAULT 0.5, is_conflict INTEGER DEFAULT 0,
            conflict_group_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            memory_kind TEXT, expires_at REAL, source TEXT DEFAULT 'manual', metadata TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_core_user ON core_memory(user_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_core_user_key ON core_memory(user_id, key)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_kind ON core_memory(user_id, memory_kind)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, summary TEXT,
            state_deltas TEXT, topics TEXT, message_count INTEGER DEFAULT 0,
            started_at REAL NOT NULL, ended_at REAL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, summary TEXT NOT NULL,
            emotional_weight REAL DEFAULT 0.5, tags TEXT, created_at REAL NOT NULL,
            memory_kind TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_episodes_kind ON episodes(user_id, memory_kind)")

    # 2. Support tables
    op.execute("""
        CREATE TABLE IF NOT EXISTS staging_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'default', session_id TEXT NOT NULL,
            event_id TEXT, content TEXT NOT NULL, importance REAL DEFAULT 0.5,
            metadata TEXT DEFAULT '{}', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS archived_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'default', original_id INTEGER,
            content TEXT NOT NULL, memory_type TEXT, importance REAL,
            archive_reason TEXT NOT NULL, archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, action TEXT NOT NULL, layer TEXT,
            target_id TEXT, details TEXT, timestamp REAL NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, timestamp REAL NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS embedding_cache (
            text_hash TEXT PRIMARY KEY, embedding BLOB NOT NULL,
            model_name TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 3. RAG
    op.execute("""
        CREATE TABLE IF NOT EXISTS rag_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL, path TEXT, content TEXT NOT NULL,
            sha256_hash TEXT, wiki_type TEXT,
            created_at REAL DEFAULT (strftime('%s','now')),
            updated_at REAL DEFAULT (strftime('%s','now'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_rag_user ON rag_pages(user_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL, bin_embedding BLOB, memory_kind TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_bin ON rag_chunks(page_id, id) WHERE bin_embedding IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_rag_chunks_page_idx ON rag_chunks(page_id, chunk_index)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS rag_relations (
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL DEFAULT 'elaborates',
            weight REAL DEFAULT 0.8,
            PRIMARY KEY (source_id, target_id, relation_type)
        )
    """)

    # 4. Graph
    op.execute("""
        CREATE TABLE IF NOT EXISTS epi_nodes (
            node_id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL, content TEXT NOT NULL,
            node_type TEXT NOT NULL, tags TEXT,
            confidence REAL DEFAULT 0.5, created_at REAL NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS epi_edges (
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            relation TEXT NOT NULL, weight REAL DEFAULT 0.8,
            created_at REAL NOT NULL,
            PRIMARY KEY (source_id, target_id, relation)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS epi_tags (
            node_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (node_id, tag)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_epi_tags_tag ON epi_tags(tag)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS temporal_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, event_type TEXT NOT NULL,
            content TEXT NOT NULL, timestamp REAL NOT NULL,
            importance REAL DEFAULT 0.5, metadata TEXT
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS temporal_links (
            from_event INTEGER NOT NULL, to_event INTEGER NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'follows',
            strength REAL DEFAULT 0.5,
            PRIMARY KEY (from_event, to_event, link_type)
        )
    """)

    # 5. Wiki
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_wiki (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, wiki_type TEXT NOT NULL,
            title TEXT NOT NULL, content TEXT NOT NULL,
            tags TEXT, importance REAL DEFAULT 0.5,
            source TEXT DEFAULT 'manual',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_wiki (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, wiki_type TEXT NOT NULL,
            title TEXT NOT NULL, content TEXT NOT NULL,
            tags TEXT, importance REAL DEFAULT 0.5,
            source TEXT DEFAULT 'manual',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS wiki_index (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL, wiki_type TEXT NOT NULL,
            title TEXT NOT NULL, file_path TEXT NOT NULL,
            tags TEXT, importance REAL DEFAULT 0.5,
            content TEXT DEFAULT '', content_hash TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_path ON wiki_index(file_path)")

    # 6. Registry
    op.execute("""
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

    op.execute("""
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

    # 7. Audit & Conflicts
    op.execute("""
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
    op.execute("CREATE INDEX IF NOT EXISTS idx_importance_audit_user ON importance_audit(user_id, rescored_at DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS memory_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, content TEXT NOT NULL,
            is_conflict INTEGER DEFAULT 0, conflict_group_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 8. Saga Idempotency Log
    op.execute("""
        CREATE TABLE IF NOT EXISTS saga_step_log (
            saga_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            result_json BLOB,
            completed_at REAL NOT NULL,
            PRIMARY KEY (saga_id, step_name, params_hash)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_saga_step_log_lookup ON saga_step_log(saga_id)")

    # 9. FTS5 (must be done statement by statement)
    op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(title, content, wiki_type, content=rag_pages, content_rowid=id)")
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS user_wiki_fts USING fts5(title, content, wiki_type, tags, content=user_wiki, content_rowid=entry_id)"
    )
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS agent_wiki_fts USING fts5(title, content, wiki_type, tags, content=agent_wiki, content_rowid=entry_id)"
    )
    op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(title, content, wiki_type, tags, content=wiki_index, content_rowid=entry_id)")


def downgrade() -> None:
    pass  # Baseline migration doesn't drop anything for safety
