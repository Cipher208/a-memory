"""A2.1 — core_memory_temporal: bi-temporal interval history

Revision ID: 20260903_1100_a21
Revises: 20260903_1000_a12
Create Date: 2026-09-03

Additive interval table (see test_bi_temporal design note): core_memory keeps
its UNIQUE(layer,user,key); the value history with valid_from/valid_to
intervals lives here. save/delete hooks maintain it; get_at_time reads it.
"""

from alembic import op

revision: str = "20260903_1100_a21"
down_revision = "20260903_1000_a12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS core_memory_temporal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            importance REAL DEFAULT 0.5,
            memory_kind TEXT,
            valid_from REAL NOT NULL,
            valid_to REAL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_core_temporal_key ON core_memory_temporal(layer, user_id, key, valid_from)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core_memory_temporal")
