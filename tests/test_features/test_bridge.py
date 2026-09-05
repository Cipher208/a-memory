"""Task 5 (Phase H): A8 MEMORY.md-бридж — regenerate топ-фактов + drain ingest."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from features.bridge import BRIDGE_MARKER, DRAIN_COMMENT
from shared.connection import connection_manager
from shared.migrations import MigrationManager

# 10 инвариантных фактов 0.30..0.84; порог 0.6 → в бридж попадают ровно топ-5
IMPORTANCES = [0.30, 0.36, 0.42, 0.48, 0.54, 0.60, 0.66, 0.72, 0.78, 0.84]


@pytest.fixture
async def cm(tmp_path, monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _seed_facts(cm: Any, user_id: str = "u1") -> None:
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=cm, layer="agent")
    await cmem._init_db()
    for i, imp in enumerate(IMPORTANCES):
        await cmem.save(user_id, f"fact:item_{i}", f"факт-строка-номер-{i}", importance=imp, memory_kind="fact")


@pytest.mark.asyncio
async def test_regenerate_top_facts_and_marker(cm: Any) -> None:
    from features.bridge import regenerate_bridge

    await _seed_facts(cm)
    path = await regenerate_bridge("u1")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert BRIDGE_MARKER in content
    for i in range(5, 10):  # топ-5 по importance присутствуют
        assert f"факт-строка-номер-{i}" in content
    for i in range(5):  # слабейшие — нет
        assert f"факт-строка-номер-{i}" not in content


@pytest.mark.asyncio
async def test_drain_ingest_captures_distills_cleans(cm: Any) -> None:
    from features.bridge import ingest_drain, regenerate_bridge

    path = await regenerate_bridge("u1")
    path.write_text(
        path.read_text(encoding="utf-8") + "помни: обязательно проверить бэкапы\n",
        encoding="utf-8",
    )

    res = await ingest_drain("u1")
    assert res["ingested"] == 1
    assert res["routes"]["l4_saved"] >= 1

    conn = await cm.get("memory.db")
    l0 = await (await conn.execute("SELECT event, raw_type, text FROM l0_journal")).fetchall()
    assert len(l0) == 1
    assert l0[0]["event"] == "bridge_drain" and l0[0]["raw_type"] == "user-message"
    l4 = await (await conn.execute("SELECT value, memory_kind FROM core_memory WHERE user_id='u1'")).fetchall()
    assert any("проверить" in r["value"] and r["memory_kind"] == "instruction" for r in l4)

    after = path.read_text(encoding="utf-8")
    assert BRIDGE_MARKER in after
    assert "проверить бэкапы" not in after  # ниже маркера очищено
    assert DRAIN_COMMENT in after  # инструкция на месте


@pytest.mark.asyncio
async def test_empty_store_creates_skeleton(cm: Any) -> None:
    from features.bridge import regenerate_bridge

    path = await regenerate_bridge("u1")
    content = path.read_text(encoding="utf-8")
    assert BRIDGE_MARKER in content
    assert "- [" not in content  # пустой магазин → скелет без строк фактов
