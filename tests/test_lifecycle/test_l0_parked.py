"""`parked` и срок для `received` — два смысла одного статуса, разведённые.

`received` значил одновременно «ждёт обработки» и «спит намеренно». Из-за второго
`l0_tiers` обещал никогда его не тирировать, и из-за этого у первого не было ни
читателя, ни срока: каждый ранний возврат копил строки вечно (353 на живой базе).
Тесты ниже фиксируют, что импорт сохраняется, а застрявшее — закрывается.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path, monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _status(cm: Any, rid: int) -> str:
    conn = await cm.get("memory.db")
    return (await (await conn.execute("SELECT status FROM l0_journal WHERE id=?", (rid,))).fetchone())[0]


@pytest.mark.asyncio
async def test_capture_can_park_a_row(cm: Any) -> None:
    """Parked is a separate status, not a flag buried in raw_type."""
    from shared.l0 import capture

    parked = await capture("import", "user", "u1", "история, которую владелица просила сохранить", raw_type="import", park=True)
    ordinary = await capture("new_message", "user", "u1", "обычное сообщение")
    assert None not in (parked, ordinary)

    assert await _status(cm, parked) == "parked"
    assert await _status(cm, ordinary) == "received"


@pytest.mark.asyncio
async def test_parked_rows_are_exempt_from_the_expiry(cm: Any) -> None:
    """The whole point of the split: an expiry must not eat a deliberate import."""
    from lifecycle.l0_tiers import close_overdue_received
    from shared.l0 import capture

    now = 1_800_000_000.0
    old_parked = await capture("import", "user", "u1", "старый импорт, уснуть навсегда", raw_type="import", park=True, ts_override=now - 400 * 86400)
    old_received = await capture("new_message", "user", "u1", "старое застрявшее сообщение", ts_override=now - 400 * 86400)
    assert None not in (old_parked, old_received)

    res = await close_overdue_received(now=now)
    assert res["closed"] == 1, res
    assert await _status(cm, old_received) == "gated_out"
    assert await _status(cm, old_parked) == "parked", "импорт обязан пережить закрытие"


@pytest.mark.asyncio
async def test_a_fresh_received_row_is_left_alone(cm: Any) -> None:
    """The expiry closes strands, not rows that are simply still new."""
    from lifecycle.l0_tiers import close_overdue_received
    from shared.l0 import capture

    now = 1_800_000_000.0
    fresh = await capture("new_message", "user", "u1", "только что пришло", ts_override=now - 3600.0)
    assert fresh is not None

    res = await close_overdue_received(now=now)
    assert res["closed"] == 0, res
    assert await _status(cm, fresh) == "received"


@pytest.mark.asyncio
async def test_the_close_records_why_and_stays_replayable(cm: Any) -> None:
    """A status alone cannot tell a deliberate close from a row nobody ever looked at.

    The recorded (gate, config_hash) pair is what makes the close reproducible: an
    unchanged config keeps it closed, a changed one re-opens it.
    """
    from lifecycle.l0_tiers import close_overdue_received
    from shared.l0 import capture

    now = 1_800_000_000.0
    rid = await capture("new_message", "user", "u1", "застрявшее сообщение", ts_override=now - 30 * 86400)
    assert rid is not None
    await close_overdue_received(now=now)

    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT status, processed_at, decisions FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row[0] == "gated_out" and row[1] == now
    decisions = json.loads(row[2])
    assert decisions[-1]["reason"] == "never_processed"
    assert decisions[-1]["gate"] == "g1" and decisions[-1]["config_hash"]


@pytest.mark.asyncio
async def test_the_close_keeps_existing_decisions(cm: Any) -> None:
    """An append, not an overwrite — a row's history is not erased by closing it."""
    from lifecycle.l0_tiers import close_overdue_received
    from shared.l0 import capture

    now = 1_800_000_000.0
    rid = await capture("new_message", "user", "u1", "строка с историей", decisions=[{"earlier": "decision"}], ts_override=now - 30 * 86400)
    assert rid is not None
    await close_overdue_received(now=now)

    conn = await cm.get("memory.db")
    decisions = json.loads((await (await conn.execute("SELECT decisions FROM l0_journal WHERE id=?", (rid,))).fetchone())[0])
    assert decisions[0] == {"earlier": "decision"}
    assert decisions[-1]["reason"] == "never_processed"


@pytest.mark.asyncio
async def test_the_ttl_is_configurable(cm: Any) -> None:
    """`config.yaml → l0.received_ttl_days`, the same mechanism as l1_buffer_size."""
    from lifecycle.l0_tiers import close_overdue_received
    from shared.l0 import capture

    now = 1_800_000_000.0
    rid = await capture("new_message", "user", "u1", "двухдневная строка", ts_override=now - 2 * 86400)
    assert rid is not None

    assert (await close_overdue_received(ttl_days=7, now=now))["closed"] == 0
    assert await _status(cm, rid) == "received"
    assert (await close_overdue_received(ttl_days=1, now=now))["closed"] == 1
    assert await _status(cm, rid) == "gated_out"


@pytest.mark.asyncio
async def test_peek_does_not_train_the_threshold(cm: Any) -> None:
    """The measured trap: 365 refused rows would drag a live threshold to the floor.

    `gate` feeds the EMA on every score. A replay walks exactly this kind of
    backlog, so `T = ALPHA*score + (1-ALPHA)*T` with ALPHA=0.1 would pull a live
    threshold of 0.240 down to the 0.1 floor — after which everything passes and
    the gate stops existing.
    """
    from shared.adaptive import AdaptiveThresholdManager

    manager = AdaptiveThresholdManager()
    manager._current_value = 0.24  # a live value; без БД get_threshold вернёт его из кэша

    verdict = await manager.peek(0.0)
    assert verdict["bypass"] is True
    assert verdict["threshold"] == 0.24
    assert await manager.get_threshold() == 0.24, "peek обязан не трогать порог"

    for _ in range(50):
        await manager.gate(0.0)
    assert await manager.get_threshold() < 0.24, "а gate, наоборот, обучает — иначе тест ничего не проверяет"


@pytest.mark.asyncio
async def test_an_unmigrated_import_survives_the_expiry(cm: Any) -> None:
    """The second line of defence: `raw_type='import'` is skipped even as `received`.

    Migration g27 renames those rows to `parked`, and this clause keeps them safe if
    that migration has not run or failed halfway. The cost of being wrong is one
    owner-requested history silently closed, so the redundant guard is worth it.
    Reverting the clause closes the row and fails here.
    """
    from lifecycle.l0_tiers import close_overdue_received
    from shared.l0 import capture

    now = 1_800_000_000.0
    # pre-g27 state: an import row still called `received`
    rid = await capture("import", "user", "u1", "импорт до миграции", raw_type="import", ts_override=now - 400 * 86400)
    assert rid is not None
    assert await _status(cm, rid) == "received"

    res = await close_overdue_received(now=now)
    assert res["closed"] == 0, res
    assert await _status(cm, rid) == "received", "импорт обязан пережить закрытие в любом состоянии"
