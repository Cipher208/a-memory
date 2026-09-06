"""S17 #5 — l0_journal content_hash: SHA-256 дедуп повторных блоков.

Повторный вывод команды (одинаковый текст в тот же layer/user за окно дедупа)
хранится один раз; повтор — нет. Hash-цепочка (tamper-evidence) НЕ дедупится:
каждая вставка получает свои hash_prev/hash_self, разрывы chain нет.
"""

import contextlib

from alembic import op

revision: str = "20260905_1600_g23"
down_revision = "20260905_1500_g22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE l0_journal ADD COLUMN content_hash TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_l0_content_hash ON l0_journal(content_hash)")


def downgrade() -> None:
    # SQLite 3.35+ DROP COLUMN; старые окружения просто оставляют колонку.
    with contextlib.suppress(Exception):
        op.execute("DROP INDEX IF EXISTS idx_l0_content_hash")
        op.execute("ALTER TABLE l0_journal DROP COLUMN content_hash")
