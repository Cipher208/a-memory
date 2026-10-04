"""g27 — rows that were waiting forever become `parked`, and `received` gets an end.

Один и тот же статус `received` означал две несовместимые вещи:

- «только что пришло, ждёт обработки» — так пишет хук на каждое сообщение;
- «намеренно спит, разбудить потом» — так лёг импорт истории
  (`scripts/l0_import_source.py`, `raw_type='import'`).

Из-за второго `lifecycle/l0_tiers` обещает, что `received` НИКОГДА не тирируется
и не архивируется, — и из-за этого у первого смысла не было ни читателя, ни
срока, и каждый ранний возврат в пути захвата копил строки вечно (353 строки на
живой базе, старейшая от 26.07).

Здесь два смысла разводятся: строки `received` с `raw_type='import'` становятся
`parked` (их владелица просила сохранить, они не застряли), а `received`
получает срок хранения `l0.received_ttl_days` в ночном проходе.

Проверено перед миграцией: на живой базе Эли все 3031 строки `received` имеют
`raw_type='import'` и пустой `decisions`, то есть застрявших строк с этим
статусом не осталось — переводить в `parked` нечего, кроме импорта.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261004_2200_g27"
down_revision: str | None = "20261004_2130_g26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Move deliberate imports from `received` to `parked`."""
    op.execute("UPDATE l0_journal SET status='parked' WHERE status='received' AND raw_type='import'")


def downgrade() -> None:
    """Return parked imports to `received` (their pre-g27 name)."""
    op.execute("UPDATE l0_journal SET status='received' WHERE status='parked'")
