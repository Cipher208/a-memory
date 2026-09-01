"""D1.14 — memory versioning: full-row ledger JSON + core_memory_snapshots.

Revision ID: 20260901_1300_d114
Revises: 20260901_1000_a22
Create Date: 2026-09-01 13:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260901_1300_d114"
down_revision: str | Sequence[str] | None = "20260901_1000_a22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE core_memory_history ADD COLUMN old_row_json TEXT")
    op.execute("ALTER TABLE core_memory_history ADD COLUMN new_row_json TEXT")
    op.execute("""
        CREATE TABLE IF NOT EXISTS core_memory_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            fact_count INTEGER NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(layer, user_id, name)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core_memory_snapshots")
    # SQLite 3.35+ supports DROP COLUMN; a22-owned table, columns were added here.
    op.execute("ALTER TABLE core_memory_history DROP COLUMN old_row_json")
    op.execute("ALTER TABLE core_memory_history DROP COLUMN new_row_json")
