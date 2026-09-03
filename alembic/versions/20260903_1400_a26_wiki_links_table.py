"""A1.4-a26 — wiki_links table into the migration chain.

Revision ID: 20260903_1400_a26
Revises: 20260903_1300_a25
Create Date: 2026-09-03

wiki_links (A1.4 back-link closure + wiki_query BFS substrate) was only
created lazily by WikiIndex.init_db() — alembic-initialized deployments
ran without it, so add_link/get_links/wiki_query hit a missing table
(audit finding #4). IF NOT EXISTS keeps lazy-create DBs untouched.
"""

import contextlib

from alembic import op

revision: str = "20260903_1400_a26"
down_revision = "20260903_1300_a25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS wiki_links (
            link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL,
            from_path TEXT NOT NULL,
            to_path TEXT NOT NULL,
            link_type TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(layer, from_path, to_path, link_type)
        )"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_wiki_links_to ON wiki_links(layer, to_path)")


def downgrade() -> None:
    with contextlib.suppress(Exception):
        op.execute("DROP INDEX IF EXISTS idx_wiki_links_to")
        op.execute("DROP TABLE IF EXISTS wiki_links")
