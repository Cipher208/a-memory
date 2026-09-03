"""A2.4 — epi_edges tags column.

Revision ID: 20260903_1200_a24
Revises: 20260903_1100_a21
Create Date: 2026-09-03

Edge metadata for traversal filters (_inverse, _value_regex) — JSON list of
strings, empty by default. All existing edges backfill to [].
"""

import contextlib

from alembic import op

revision: str = "20260903_1200_a24"
down_revision = "20260903_1100_a21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with contextlib.suppress(Exception):
        op.execute("ALTER TABLE epi_edges ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")  # column already added


def downgrade() -> None:
    with contextlib.suppress(Exception):
        op.execute("ALTER TABLE epi_edges DROP COLUMN tags")
