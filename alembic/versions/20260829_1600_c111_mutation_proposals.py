"""Phase C C1.11 — mutation_proposals (staged mutation: proposal → review → apply).

Revision ID: 20260829_1600_c111
Revises: 20260829_1300_d1c110
Create Date: 2026-08-29 16:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_1600_c111"
down_revision: str | Sequence[str] | None = "20260829_1300_d1c110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS mutation_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default',
            layer TEXT NOT NULL DEFAULT 'user',
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            proposed_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            decided_at REAL,
            decided_by TEXT,
            result_ref TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_proposals_status ON mutation_proposals(status, user_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_proposals_status")
    op.execute("DROP TABLE IF EXISTS mutation_proposals")
