"""Task 6 (Phase F): watermark + replay — повторный прогон G1 по окну L0."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


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

    res = await replay(since_days=1)
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
    res2 = await replay(since_days=1)
    assert res2 == {"processed": 0, "skipped": 0, "conflicts": 0}


@pytest.mark.asyncio
async def test_replay_reruns_only_reset_rows(cm: Any) -> None:
    from features.replay import replay
    from shared.l0 import capture

    rid1 = await capture("new_message", "user", "u1", "я решила перейти на PostgreSQL для проекта")
    rid2 = await capture("new_message", "user", "u1", "наблюдение: трафик растёт по пятницам стабильно")
    rid3 = await capture("new_message", "user", "u1", "я решил включить wal режим в базе данных")
    assert None not in (rid1, rid2, rid3)
    first = await replay(since_days=1)
    assert first["processed"] == 3

    # пере-открыть часть окна (сброс watermark): статус + decisions очищены
    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status='gated_out', decisions='[]' WHERE id IN (?, ?)", (rid1, rid2))
    await conn.commit()

    res = await replay(since_days=1)
    assert res["processed"] == 2 and res["skipped"] == 0
    rows = {r["id"]: r for r in await _rows(cm)}
    assert rows[rid1]["status"] != "gated_out" and rows[rid2]["status"] != "gated_out"
    assert rows[rid3]["status"] in {"promoted_l4", "saved_l3"}  # не тронут

    # и снова идемпотентно
    assert (await replay(since_days=1))["processed"] == 0

    # decisions-защита: gated_out-строка с СОХРАНЁННЫМ решением этого конфига — skip
    await conn.execute("UPDATE l0_journal SET status='gated_out' WHERE id=?", (rid1,))
    await conn.commit()
    res2 = await replay(since_days=1)
    assert res2["processed"] == 0 and res2["skipped"] == 1
    rows2 = {r["id"]: r for r in await _rows(cm)}
    assert rows2[rid1]["status"] == "gated_out"  # не переобработана
