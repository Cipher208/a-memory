"""A2.1: bi-temporal facts — interval history table + time-scoped queries.

Design note: core_memory keeps UNIQUE(layer,user,key) — every consumer
(get/search/inject/ACT-R) assumes key-unique rows. The bi-temporal chain
therefore lives in an additive `core_memory_temporal` table (intervals per
key), maintained by save/update/delete hooks. `get_at_time` answers
"what was true at time T". No hot-path schema change.
"""

import asyncio
import sqlite3
import time

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic_core(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


def _intervals(db_path, key):
    conn = sqlite3.connect(db_path / "memory.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM core_memory_temporal WHERE key=? ORDER BY valid_from", (key,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def test_migration_creates_temporal_table(hermetic_core):
    conn = sqlite3.connect(hermetic_core / "memory.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(core_memory_temporal)").fetchall()}
    conn.close()
    assert "core_memory_temporal" in tables
    assert {"layer", "user_id", "key", "value", "valid_from", "valid_to"} <= cols


async def test_save_opens_interval_and_rewrite_chains(hermetic_core):
    from core.memory import CoreMemory

    cm = CoreMemory(cm=connection_manager, layer="user")
    await cm.save("u1", "pref", "dark mode", importance=0.5)
    await asyncio.sleep(0.02)
    await cm.save("u1", "pref", "light mode", importance=0.5)

    intervals = _intervals(hermetic_core, "pref")
    assert len(intervals) == 2
    assert intervals[0]["value"] == "dark mode" and intervals[0]["valid_to"] is not None
    assert intervals[1]["value"] == "light mode" and intervals[1]["valid_to"] is None
    # chain: the second interval starts at/after the first closes
    assert intervals[1]["valid_from"] >= intervals[0]["valid_to"]


async def test_get_at_time(hermetic_core):
    from core.memory import CoreMemory

    cm = CoreMemory(cm=connection_manager, layer="user")
    await cm.save("u1", "pref", "dark mode", importance=0.5)
    await asyncio.sleep(0.05)
    t_between = time.time()
    await asyncio.sleep(0.05)
    await cm.save("u1", "pref", "light mode", importance=0.5)
    await asyncio.sleep(0.05)

    at_old = await cm.get_at_time("u1", "pref", t_between)
    at_now = await cm.get_at_time("u1", "pref", time.time())
    at_never = await cm.get_at_time("u1", "pref", 0)
    assert at_old is not None and at_old["value"] == "dark mode"
    assert at_now is not None and at_now["value"] == "light mode"
    assert at_never is None


async def test_delete_closes_interval(hermetic_core):
    from core.memory import CoreMemory

    cm = CoreMemory(cm=connection_manager, layer="user")
    await cm.save("u1", "gone", "value", importance=0.5)
    await cm.delete("u1", "gone")
    intervals = _intervals(hermetic_core, "gone")
    assert len(intervals) == 1 and intervals[0]["valid_to"] is not None
    assert await cm.get_at_time("u1", "gone", time.time()) is None


async def test_temporal_write_failure_never_breaks_save(hermetic_core):
    """Ledger discipline: history is advisory, memory writes must not fail.

    Real failure mode: a pre-A2.1 DB has no core_memory_temporal table.
    """
    from core.memory import CoreMemory

    conn = sqlite3.connect(hermetic_core / "memory.db")
    conn.execute("DROP TABLE core_memory_temporal")
    conn.commit()
    conn.close()

    cm = CoreMemory(cm=connection_manager, layer="user")
    entry_id = await cm.save("u1", "still saved", "value", importance=0.5)
    assert entry_id > 0
    row = await cm.get("u1", "still saved")
    assert row is not None and row.value == "value"


async def test_search_hides_earlier_even_off_page(hermetic_core):
    """B2 is_current-view: earlier скрыта, если later существует ГЛОБАЛЬНО.

    Пара C4 (как пишет дистиллятор): earlier 'fact:x', later 'fact:x::v1'
    с contradicts. Запрос матчит только 'старое значение' — later вообще
    не попадает на страницу выдачи (12 филлеров matched=1/imp=1.0
    выталкивают matched=1/imp=0.9 за limit=10). S2 on-page fusion тут
    бессильна — пара не сошлась в picked; B2 закрывает earlier по
    факту существования later в БД. include_superseded=True возвращает
    скрытую строку с is_current=False.
    """
    from core.memory import CoreMemory

    cm = CoreMemory(cm=connection_manager, layer="user")
    await cm.save("b2u", "fact:x", "старое значение", importance=0.9, metadata={"scope": "earlier"})
    await cm.save("b2u", "fact:x::v1", "новое значение", importance=0.9, metadata={"scope": "later", "contradicts": "fact:x"})
    for n in range(12):
        await cm.save("b2u", f"filler:{n}", f"значение filler {n}", importance=1.0)

    out = await cm.search("b2u", "старое значение", limit=10)
    assert not any(i["value"].startswith("старое") for i in out), f"earlier протекла: {out}"
    assert all(i["is_current"] for i in out), f"каждый item несёт is_current=True: {out}"

    full = await cm.search("b2u", "старое значение", limit=10, include_superseded=True)
    hidden = [i for i in full if not i["is_current"]]
    assert hidden and hidden[0]["value"].startswith("старое"), f"include_superseded=True вернул скрытую: {full}"
