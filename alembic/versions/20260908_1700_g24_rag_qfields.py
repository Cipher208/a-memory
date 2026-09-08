"""g24 — rag_pages.qfields_json: doc2query-поля для лексического моста запрос→юнит.

CLACK exp3 (2026-09-08): 3 гипотетических запроса на юнит + буст
2.0·|qt ∩ qf| подняли hit@5 родного ретривера с 0.08 до 0.46 на
LongMemEval-S. Колонка хранит JSON-массив строк; пишет её ночная
джоба lifecycle/qfields.qfield_enrich (flag retrieval.qfields.enabled,
config-only), читает буст в rag/dual_route.
"""

import contextlib

from alembic import op

revision: str = "20260908_1700_g24"
down_revision = "20260905_1600_g23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE rag_pages ADD COLUMN qfields_json TEXT")


def downgrade() -> None:
    # SQLite 3.35+ DROP COLUMN; старые окружения просто оставляют колонку.
    with contextlib.suppress(Exception):
        op.execute("ALTER TABLE rag_pages DROP COLUMN qfields_json")
