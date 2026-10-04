"""g26 — l0_cold_archive keeps the row's hash-chain links.

Тирирование переносит обработанную строку старше 180 дней в `l0_cold_archive` и
УДАЛЯЕТ её из `l0_journal`. Хеш-цепочка (`verify_chain`) считалась по одному
журналу, поэтому вместе со строкой пропадало её звено: следующая уцелевшая
строка несла `hash_prev`, указывающий на строку, которой больше нет, и проверка
объявляла подлогом само тирирование.

Колонки едут вместе со строкой, и `verify_chain` обходит объединение журнала и
архива в порядке id. Живые базы до этой миграции архива не имеют (он пуст), так
что заполнять нечего.

`_ensure_schema` в `lifecycle/l0_tiers.py` добавляет те же колонки для баз,
которых миграция ещё не коснулась.
"""

import contextlib

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261004_2130_g26"
down_revision: str | None = "20261004_1300_door_rejections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the chain links to the cold archive."""
    for ddl in (
        "ALTER TABLE l0_cold_archive ADD COLUMN hash_prev TEXT",
        "ALTER TABLE l0_cold_archive ADD COLUMN hash_self TEXT",
    ):
        with contextlib.suppress(Exception):  # column already exists
            op.execute(ddl)


def downgrade() -> None:
    """Drop the chain links (SQLite 3.35+; older engines keep the columns)."""
    with contextlib.suppress(Exception):
        op.execute("ALTER TABLE l0_cold_archive DROP COLUMN hash_self")
        op.execute("ALTER TABLE l0_cold_archive DROP COLUMN hash_prev")
