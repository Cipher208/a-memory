"""a3 — the bin remembers when an archived row happened.

`archived_memories` kept only `archived_at`, the moment a row was removed, so a
restored episode was stamped with the moment of its return: it never aged, never
met the purge, and sorted to the end of the L4 queue (`created_at` order).

`ArchivedMemories.ensure()` adds this column on bases that never run migrations —
it is called from constructors that do not migrate. This revision puts the same
column in the chain so `alembic upgrade head` alone yields the schema the code
expects.

SQLite's ADD COLUMN is a schema-only change: no existing row is rewritten, and
every old row reads NULL (the time was never recorded, so NULL is the truth, not
a gap). The existence guard makes it a no-op when `ensure()` reached the base
first — a script or autohook can have run it before the server migrated.
"""

from collections.abc import Sequence

import sqlalchemy
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261103_1200_a3"
down_revision: str | Sequence[str] | None = "20261004_2200_g27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn: sqlalchemy.Connection, table: str, column: str) -> bool:
    rows = conn.execute(sqlalchemy.text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def upgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, "archived_memories", "original_created_at"):
        op.execute("ALTER TABLE archived_memories ADD COLUMN original_created_at REAL")


def downgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn, "archived_memories", "original_created_at"):
        op.execute("ALTER TABLE archived_memories DROP COLUMN original_created_at")
