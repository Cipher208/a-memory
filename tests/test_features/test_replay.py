"""Task 6 (Phase F): watermark + replay — повторный прогон G1 по окну L0."""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager

# Текст, который гейт важности пропускает (score 0.400 при пороге 0.3). Тесты о
# самом гейте обязаны на нём стоять, иначе они проверяют отказ, а не поведение.
PASSING_TEXT = (
    "Важно: я решила перейти на PostgreSQL для проекта X, потому что MySQL не держит нагрузку"
    " и падает на 500 rps. Решение окончательное, миграцию делаем до 20.10."
)


@pytest.fixture
async def cm(tmp_path, monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _rows(cm: Any) -> list[Any]:
    conn = await cm.get("memory.db")
    return list(await (await conn.execute("SELECT id, status, processed_at, decisions FROM l0_journal ORDER BY id")).fetchall())


@pytest.mark.asyncio
async def test_replay_processes_window_then_idempotent(cm: Any) -> None:
    from features.replay import replay
    from shared.l0 import capture

    rid1 = await capture("new_message", "user", "u1", "я решила перейти на PostgreSQL для проекта")
    rid2 = await capture("new_message", "user", "u1", "наблюдение: трафик растёт по пятницам стабильно")
    rid3 = await capture("new_message", "user", "u1", "я решил включить wal режим в базе данных")
    assert None not in (rid1, rid2, rid3)

    # respect_gate=False: тест про МЕХАНИЗМ повторного прогона, а не про отбор.
    res = await replay(since_days=1, respect_gate=False)
    assert res["processed"] == 3
    assert res["skipped"] == 0

    rows = await _rows(cm)
    assert {r["status"] for r in rows} <= {"promoted_l4", "saved_l3"}
    assert all(r["processed_at"] is not None for r in rows)
    assert all('"gate": "g1"' in r["decisions"] for r in rows)
    # дистиллятор реально отработал: инварианты легли в L4
    conn = await cm.get("memory.db")
    l4 = await (await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id='u1'")).fetchone()
    assert int(l4[0]) >= 1

    # идемпотентность: тот же config-hash → no-op (обработанные строки вне выборки статусов)
    res2 = await replay(since_days=1, respect_gate=False)
    # Полный словарь проверяется по значимым ключам: `gated` и `limit` описывают
    # прогон, а не окно, и появляются в отчёте всегда.
    assert (res2["processed"], res2["skipped"], res2["conflicts"]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_replay_reruns_only_reset_rows(cm: Any) -> None:
    from features.replay import replay
    from shared.l0 import capture

    rid1 = await capture("new_message", "user", "u1", "я решила перейти на PostgreSQL для проекта")
    rid2 = await capture("new_message", "user", "u1", "наблюдение: трафик растёт по пятницам стабильно")
    rid3 = await capture("new_message", "user", "u1", "я решил включить wal режим в базе данных")
    assert None not in (rid1, rid2, rid3)
    first = await replay(since_days=1, respect_gate=False)
    assert first["processed"] == 3

    # пере-открыть часть окна (сброс watermark): статус + decisions очищены
    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status='gated_out', decisions='[]' WHERE id IN (?, ?)", (rid1, rid2))
    await conn.commit()

    res = await replay(since_days=1, respect_gate=False)
    assert res["processed"] == 2 and res["skipped"] == 0
    rows = {r["id"]: r for r in await _rows(cm)}
    assert rows[rid1]["status"] != "gated_out" and rows[rid2]["status"] != "gated_out"
    assert rows[rid3]["status"] in {"promoted_l4", "saved_l3"}  # не тронут

    # и снова идемпотентно
    assert (await replay(since_days=1, respect_gate=False))["processed"] == 0

    # decisions-защита: gated_out-строка с СОХРАНЁННЫМ решением этого конфига — skip
    await conn.execute("UPDATE l0_journal SET status='gated_out' WHERE id=?", (rid1,))
    await conn.commit()
    res2 = await replay(since_days=1, respect_gate=False)
    assert res2["processed"] == 0 and res2["skipped"] == 1
    rows2 = {r["id"]: r for r in await _rows(cm)}
    assert rows2[rid1]["status"] == "gated_out"  # не переобработана


@pytest.mark.asyncio
async def test_replay_by_ids_touches_nothing_else(cm: Any) -> None:
    """The named-row escape hatch: wake one row, leave every other row alone.

    Why it is needed rather than a convenience: `replay` distils whatever it
    selects and bypasses the importance gate, so replaying a stranded backlog by
    window would put the chatter that gate refused straight back into memory.
    """
    from features.replay import replay
    from shared.l0 import capture

    wanted = await capture("new_message", "user", "u1", "я решила перейти на PostgreSQL для проекта")
    untouched = await capture("new_message", "user", "u1", "наблюдение: трафик растёт по пятницам стабильно")
    also_untouched = await capture("new_message", "user", "u1", "просто болтовня ни о чём особенном")
    assert None not in (wanted, untouched, also_untouched)

    res = await replay(since_days=1, ids=[wanted])
    assert res["processed"] == 1, res

    by_id = {r["id"]: r for r in await _rows(cm)}
    assert by_id[wanted]["status"] in {"promoted_l4", "saved_l3"}, by_id[wanted]
    assert by_id[untouched]["status"] == "received", "a row not named must not be distilled"
    assert by_id[also_untouched]["status"] == "received", "a row not named must not be distilled"


@pytest.mark.asyncio
async def test_replay_by_ids_ignores_the_status_filter(cm: Any) -> None:
    """A closed row can be re-opened by id — that is how a deliberate close is undone.

    The window filter only accepts 'received'/'gated_out', so a row already closed
    as `routed_direct` is invisible to it. Naming the id must reach it anyway;
    otherwise a wrongly-closed row could never be reconsidered.
    """
    from features.replay import replay
    from shared.connection import connection_manager
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", "я решила перейти на PostgreSQL для проекта")
    assert rid is not None
    conn = await connection_manager.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status='routed_direct' WHERE id=?", (rid,))
    await conn.commit()

    res = await replay(since_days=1, ids=[rid])
    assert res["processed"] == 1, res
    assert (await _rows(cm))[0]["status"] in {"promoted_l4", "saved_l3"}


@pytest.mark.asyncio
async def test_replay_respects_the_importance_gate(cm: Any) -> None:
    """A replay must not re-admit what the live gate refused.

    This is the reason the gate is consulted: `replay` used to distil every
    selected row, so a message the hook refused was written to memory by a replay
    — the two paths disagreed about the same text. That was survivable while a
    human typed every replay and became wrong the moment the nightly pass called
    it. The text below is the kind the gate exists to refuse.
    """
    from features.replay import replay
    from shared.l0 import capture

    chatter = await capture("new_message", "user", "u1", "хихикаю")
    assert chatter is not None

    res = await replay(since_days=1)
    assert res["gated"] == 1, res
    assert res["processed"] == 0, res

    row = (await _rows(cm))[0]
    assert row["status"] == "gated_out"
    decisions = json.loads(row["decisions"])
    assert decisions[-1]["reason"] == "importance_gate_bypass"
    assert "threshold" in decisions[-1]

    conn = await cm.get("memory.db")
    l4 = await (await conn.execute("SELECT COUNT(*) FROM core_memory")).fetchone()
    assert int(l4[0]) == 0, "отказная строка не должна была ничего записать"


@pytest.mark.asyncio
async def test_naming_a_row_overrides_the_gate_on_the_record(cm: Any) -> None:
    """`--ids` is a person saying "this one"; the record must say so too.

    Without the marker a later reader would conclude the gate admitted the row,
    which is the opposite of what happened.
    """
    from features.replay import replay
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", "хихикаю")
    assert rid is not None

    res = await replay(since_days=1, ids=[rid])
    assert res["processed"] == 1, res
    assert res["gated"] == 0, res

    decisions = json.loads((await _rows(cm))[0]["decisions"])
    assert decisions[-1].get("gate_override") is True


@pytest.mark.asyncio
async def test_a_window_run_records_no_override(cm: Any) -> None:
    """The mirror of the previous test: automation never claims a human chose."""
    from features.replay import replay
    from shared.l0 import capture

    rid = await capture(
        "new_message",
        "user",
        "u1",
        "Важно: я решила перейти на PostgreSQL для проекта X, потому что MySQL не держит нагрузку и падает на 500 rps. Решение окончательное, миграцию делаем до 20.10.",
    )
    assert rid is not None
    res = await replay(since_days=1)
    assert res["processed"] == 1, res

    decisions = json.loads((await _rows(cm))[0]["decisions"])
    assert "gate_override" not in decisions[-1]


@pytest.mark.asyncio
async def test_replay_is_bounded_by_max_rows(cm: Any) -> None:
    """A config change re-opens the whole backlog; one night must not distil it all.

    A changed config_hash re-opens every row that recorded the old one — 564
    refused rows on a live base — so an unbounded automated run would land the
    entire backlog in memory at once.
    """
    from features.replay import replay
    from shared.l0 import capture

    for i in range(5):
        await capture("new_message", "user", "u1", f"{PASSING_TEXT} пункт {i}")
    res = await replay(since_days=1, max_rows=2)
    assert res["processed"] == 2, res
    assert res["limit"] == 2

    statuses = [r["status"] for r in await _rows(cm)]
    assert statuses.count("received") == 3, "остальные обязаны остаться нетронутыми"
