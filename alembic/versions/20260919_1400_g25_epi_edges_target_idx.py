"""epi_edges target index for lateral inhibition.

Revision ID: 20260919_1400_g25_epi_edges_target_idx
Revises: 20260908_1700_g24
Create Date: 2026-09-19 14:00:00.000000

19.09 (issue G): lateral_inhibition scans epi_edges with
(source_id=? OR target_id=?) — the PK covers source_id, but the
target_id half was a full-table scan (640k+ rows, twice per edge).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260919_1400_g25_epi_edges_target_idx"
down_revision: str | Sequence[str] | None = "20260908_1700_g24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE INDEX IF NOT EXISTS idx_epi_edges_target ON epi_edges(target_id)")
    op.execute("ANALYZE epi_edges")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_epi_edges_target")
