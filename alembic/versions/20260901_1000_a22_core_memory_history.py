"""A2.2 — core_memory_history: L4 mutation ledger (D1.11 branches, D1.14 snapshots).

Revision ID: 20260901_1000_a22
Revises: 20260830_1000_d116
Create Date: 2026-09-01 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260901_1000_a22"
down_revision: str | Sequence[str] | None = "20260830_1000_d116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS core_memory_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            old_importance REAL,
            new_importance REAL,
            commit_hash TEXT NOT NULL,
            triggered_by TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_core_history_lookup ON core_memory_history(layer, user_id, key, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_core_history_lookup")
    op.execute("DROP TABLE IF EXISTS core_memory_history")
