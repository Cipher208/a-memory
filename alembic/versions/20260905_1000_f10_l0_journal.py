"""F1 — l0_journal: raw append-only intake."""

import contextlib

from alembic import op

revision: str = "20260905_1000_f10"
down_revision = "20260903_1400_a26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS l0_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        event TEXT NOT NULL,
        source_msg_id INTEGER,
        layer TEXT NOT NULL DEFAULT 'user',
        user_id TEXT NOT NULL DEFAULT 'default',
        text TEXT NOT NULL,
        raw_type TEXT NOT NULL DEFAULT 'plain',
        status TEXT NOT NULL DEFAULT 'received',
        decisions TEXT NOT NULL DEFAULT '[]',
        processed_at REAL,
        hash_prev TEXT NOT NULL DEFAULT '',
        hash_self TEXT NOT NULL DEFAULT ''
    );
    """)
    # sqlite3 драйвер не ест несколько statement'ов за один execute —
    # каждый DDL отдельно (в плане был один op.execute с 3 statement'ами)
    op.execute("CREATE INDEX IF NOT EXISTS idx_l0_user_ts ON l0_journal(user_id, ts)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_l0_status ON l0_journal(status)")


def downgrade() -> None:
    with contextlib.suppress(Exception):
        op.execute("DROP TABLE IF EXISTS l0_journal")
