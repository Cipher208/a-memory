"""Spec [S5]: блок insight при старте сессии списывает бюджет."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def seeded(tmp_path) -> AsyncIterator[Any]:
    from shared.connection import connection_manager

    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    from shared.migrations import MigrationManager

    await MigrationManager(cm=connection_manager).migrate()
    from graph.epistemic import EpistemicGraph

    g = EpistemicGraph(cm=connection_manager, layer="user")
    nid = await g.add_node("gu", "injected theme node", "fact")
    await g.add_edge(nid, nid, "mentions")
    # один insight с известным content, привязанный к user gu
    from shared.constants import DB_NAME
    from shared.write_dispatch import get_write_dispatch

    await get_write_dispatch().write(
        DB_NAME,
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
        " VALUES ('user', 'gu', 'insight (9 узлов): бэкап, master', 'insight', '[]', 0.6, 0.0)",
    )
    yield connection_manager
    connection_manager._conns.clear()


class _L1:
    def get_recent(self, n: int = 10) -> list:
        return []


class _L3:
    async def search_by_tag(self, user_id: str, tag: str, limit: int = 10) -> list:
        return []


class _L4:
    async def get_all(self, user_id: str, limit: int = 50) -> list:
        return []

    async def get_pinned(self, user_id: str, limit: int = 10) -> list:
        return []


class _FakeMem:
    """Same local-fake shape as the other inject test files (no shared conftest)."""

    l1 = _L1()
    l3 = _L3()
    l4 = _L4()


async def test_insight_block_present_and_debits_budget(seeded: Any) -> None:
    from features.inject import build_inject_blocks

    mem = _FakeMem()
    blocks = await build_inject_blocks(mem, None, "gu", budget=10_000)
    kinds = [b["kind"] for b in blocks]
    assert "insight" in kinds, blocks
    content = next(b["content"] for b in blocks if b["kind"] == "insight")
    assert "бэкап, master" in content
    # бюджет списан: used = budget - остатка (через return-значение невозможно,
    # проверяем прямым вызовом хелпера инварианта: маленький бюджет → нет блока)
    small = await build_inject_blocks(mem, None, "gu", budget=1)
    assert "insight" not in [b["kind"] for b in small], small
