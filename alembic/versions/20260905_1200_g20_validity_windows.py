"""G2.0 — epi_edges validity windows + status.

Revision ID: 20260905_1200_g20
Revises: 20260905_1000_f10
Create Date: 2026-09-05

Validity windows (StateMem-style): NULL valid_from/valid_to = бессрочно.
status ('active'|'expired') — материализованный вердикт recheck, обновляется
graph_enrich (O(|E|) прогон) и учитывается фильтром active_edges_clause().
"""

import contextlib

from alembic import op

revision: str = "20260905_1200_g20"
down_revision = "20260905_1000_f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Каждый DDL отдельным execute — sqlite3-драйвер не ест несколько за раз.
    for ddl in (
        "ALTER TABLE epi_edges ADD COLUMN valid_from REAL",
        "ALTER TABLE epi_edges ADD COLUMN valid_to REAL",
        "ALTER TABLE epi_edges ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
    ):
        with contextlib.suppress(Exception):  # колонка уже добавлена (init_db для живых БД)
            op.execute(ddl)


def downgrade() -> None:
    for ddl in (
        "ALTER TABLE epi_edges DROP COLUMN status",
        "ALTER TABLE epi_edges DROP COLUMN valid_to",
        "ALTER TABLE epi_edges DROP COLUMN valid_from",
    ):
        with contextlib.suppress(Exception):
            op.execute(ddl)
