"""G1 distiller: atomize → canonical key → kind-routing (инвариант→L4, событие→L3)."""

from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


class FakeL3:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, float, list[str]]] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        self.saved.append((user_id, summary, weight, tags))
        return len(self.saved)


class FakeMem:
    """Ровно то, что distill_and_route трогает на mem: l3.save (+ опц. _cm)."""

    def __init__(self) -> None:
        self.l3 = FakeL3()


@pytest.fixture
async def cm(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.mark.asyncio
async def test_invariant_routes_to_l4_event_to_l3(cm) -> None:
    from lifecycle.distiller import distill_and_route

    fake_mem, fake_graph = FakeMem(), MagicMock()
    # инвариант: «решила» = decision → L4
    r1 = await distill_and_route(fake_mem, fake_graph, "u1", "я решила перейти на PostgreSQL", 0.8)
    assert r1["l4_saved"] >= 1
    # событие: наблюдение/факт с decay > порога → L3 через mem.l3.save
    r2 = await distill_and_route(fake_mem, fake_graph, "u1", "наблюдение: трафик растёт по пятницам", 0.6)
    assert r2["l3_saved"] >= 1
    assert fake_mem.l3.saved, "event atom must reach mem.l3.save"
    rows = await (await (await cm.get("memory.db")).execute("SELECT key FROM core_memory WHERE user_id='u1'")).fetchall()
    assert all(not k[0].startswith("staging_") for k in rows), "ключи канонические, не обрубки"


@pytest.mark.asyncio
async def test_conflict_not_silent_update(cm) -> None:
    from lifecycle.distiller import distill_and_route

    fake_mem, fake_graph = FakeMem(), MagicMock()
    await distill_and_route(fake_mem, fake_graph, "u1", "база проекта: PostgreSQL", 0.8)
    r2 = await distill_and_route(fake_mem, fake_graph, "u1", "база проекта: MySQL", 0.8)
    assert r2["conflicts"] >= 1  # второе противоречит первому — запись с флагом, не затирание
