"""Phase D D1.16 — reflections (meta-memories: higher-order insights).

Revision ID: 20260830_1000_d116
Revises: 20260829_1900_d35_rehydrate
Create Date: 2026-08-30 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260830_1000_d116"
down_revision: str | Sequence[str] | None = "20260829_1900_d35_rehydrate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS reflections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            layer TEXT NOT NULL DEFAULT 'user',
            topic TEXT,
            insight TEXT NOT NULL,
            stats_json TEXT,
            created_at REAL NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_reflections_user_time ON reflections(user_id, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_reflections_user_time")
    op.execute("DROP TABLE IF EXISTS reflections")
