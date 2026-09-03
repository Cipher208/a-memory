"""A2.5-a25 — preferences table in the migration chain.

Revision ID: 20260903_1300_a25
Revises: 20260903_1200_a24
Create Date: 2026-09-03

`preferences` was a pre-v8 legacy table that survived the alembic cutover in
existing deployments but was never in the chain — a freshly migrated DB
broke AdaptiveThresholdManager on its first write (audit finding #3).
CREATE ... IF NOT EXISTS: legacy DBs keep their rows, fresh ones get the
table at the right point in the chain.
"""

from alembic import op

revision: str = "20260903_1300_a25"
down_revision = "20260903_1200_a24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL,
            updated_at REAL
        )"""
    )


def downgrade() -> None:
    # Legacy deployments carried real rows before this migration existed —
    # keep the table on downgrade, dropping it could destroy data.
    pass
