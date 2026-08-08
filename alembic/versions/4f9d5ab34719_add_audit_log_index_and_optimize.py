"""add_audit_log_index_and_optimize

Revision ID: 4f9d5ab34719
Revises: a38d67fcd99e
Create Date: 2026-08-08 01:01:58.487969

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f9d5ab34719"
down_revision: str | Sequence[str] | None = "a38d67fcd99e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Performance index for rescoring and adaptive threshold
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_lookup ON audit_log(action, layer, target_id)")

    # 2. SQLite Statistics Optimization
    op.execute("ANALYZE")

    # 3. Clean up orphans from previous migrations (defensive)
    op.execute("DELETE FROM core_memory WHERE user_id IS NULL OR value IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_audit_lookup")
