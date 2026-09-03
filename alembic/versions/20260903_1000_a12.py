"""A1.2 — wiki lifecycle status column.

Revision ID: 20260903_1000_a12
Revises: 20260901_1300_d114
Create Date: 2026-09-03

status: active | stale | archived (default 'active').
Wiki round-trip previously dropped arbitrary frontmatter keys like status.
"""

import contextlib

from alembic import op

revision: str = "20260903_1000_a12"
down_revision = "20260901_1300_d114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with contextlib.suppress(Exception):
        op.execute("ALTER TABLE wiki_index ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")


def downgrade() -> None:
    with contextlib.suppress(Exception):
        op.execute("ALTER TABLE wiki_index DROP COLUMN status")
