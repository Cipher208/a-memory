"""Layer-isolate L3/L4: add layer column to core_memory and episodes.

Revision ID: c7e21a94b0d5
Revises: 4f9d5ab34719
Create Date: 2026-08-24 11:20:00.000000

Existing rows predate layer isolation (agent/user shared one namespace);
they are all attributed to the 'user' layer. The old unique index
(user_id, key) is replaced by (layer, user_id, key) so agent and user
facts with the same key no longer overwrite each other.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "c7e21a94b0d5"
down_revision: str | Sequence[str] | None = "4f9d5ab34719"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, "core_memory", "layer"):
        # SQLite ALTER TABLE ADD COLUMN cannot use a UNIQUE index directly;
        # index swap happens below.
        op.execute("ALTER TABLE core_memory ADD COLUMN layer TEXT NOT NULL DEFAULT 'user'")

    if not _column_exists(conn, "episodes", "layer"):
        op.execute("ALTER TABLE episodes ADD COLUMN layer TEXT NOT NULL DEFAULT 'user'")

    # Replace the layer-blind unique constraint.
    op.execute("DROP INDEX IF EXISTS idx_core_user_key")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_core_layer_user_key ON core_memory(layer, user_id, key)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_episodes_layer ON episodes(layer, user_id)")

    # Defensive: any row that slipped through without attribution.
    op.execute("UPDATE core_memory SET layer='user' WHERE layer IS NULL OR layer=''")
    op.execute("UPDATE episodes SET layer='user' WHERE layer IS NULL OR layer=''")
    op.execute("ANALYZE")


def downgrade() -> None:
    conn = op.get_bind()
    op.execute("DROP INDEX IF EXISTS idx_core_layer_user_key")
    op.execute("DROP INDEX IF EXISTS idx_episodes_layer")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_core_user_key ON core_memory(user_id, key)")
    for table in ("core_memory", "episodes"):
        if _column_exists(conn, table, "layer"):
            op.execute(text(f"ALTER TABLE {table} DROP COLUMN layer"))
