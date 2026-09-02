"""A1.2 — wiki lifecycle status column

Revision ID: 20260903_1000_a12
Revises: 20260901_1300_d114
Create Date: 2026-09-03

status: active | stale | archived (default 'active').
Wiki round-trip previously dropped arbitrary frontmatter keys like status.
"""

from alembic import op

revision: str = "20260903_1000_a12"
down_revision = "20260901_1300_d114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.execute("ALTER TABLE wiki_index ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    except Exception:
        # Column already added (self-healing path or re-run)
        pass


def downgrade() -> None:
    try:
        op.execute("ALTER TABLE wiki_index DROP COLUMN status")
    except Exception:
        pass
