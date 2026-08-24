"""Rag_chunks float_embedding column (keep_float_blobs storage).

Revision ID: e5b7d2f8a1c3
Revises: c7e21a94b0d5
Create Date: 2026-08-24 14:05:00.000000

Adds the dense-embedding blob column gated by rag.storage.keep_float_blobs.
Used for supervised threshold training; search runs on binary embeddings.
"""

from collections.abc import Sequence

import contextlib
from collections.abc import Sequence

import sqlalchemy
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b7d2f8a1c3"
down_revision: str | Sequence[str] | None = "c7e21a94b0d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn: sqlalchemy.Connection, table: str, column: str) -> bool:
    rows = conn.execute(sqlalchemy.text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def upgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, "rag_chunks", "float_embedding"):
        op.execute("ALTER TABLE rag_chunks ADD COLUMN float_embedding BLOB")


def downgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn, "rag_chunks", "float_embedding"):
        # SQLite 3.35+ supports DROP COLUMN; guard anyway.
        with contextlib.suppress(Exception):
            op.execute(sqlalchemy.text("ALTER TABLE rag_chunks DROP COLUMN float_embedding"))
