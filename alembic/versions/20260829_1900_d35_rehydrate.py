"""Phase D D3.5 — compaction_events (compaction drift log for rehydrate).

Revision ID: 20260829_1900_d35_rehydrate
Revises: 20260829_1600_c111
Create Date: 2026-08-29 19:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_1900_d35_rehydrate"
down_revision: str | Sequence[str] | None = "20260829_1600_c111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS compaction_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            old_session_id TEXT,
            new_session_id TEXT,
            reason TEXT,
            summary TEXT,
            created_at REAL NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_compaction_user_time ON compaction_events(user_id, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_compaction_user_time")
    op.execute("DROP TABLE IF EXISTS compaction_events")
