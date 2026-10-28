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
    # Three current insights for gu; a different scope must not leak in.
    from shared.constants import DB_NAME
    from shared.write_dispatch import get_write_dispatch

    dispatch = get_write_dispatch()
    for layer, user_id, content in (
        ("user", "gu", "insight (9 узлов): бэкап, master"),
        ("user", "gu", "insight (7 узлов): postgres, phone"),
        ("user", "gu", "insight (5 узлов): прогулка, парк"),
        ("agent", "gu", "insight (99 узлов): чужой слой"),
    ):
        await dispatch.write(
            DB_NAME,
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, 'insight', '[]', 0.6, 0.0)",
            (layer, user_id, content),
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


async def test_insight_blocks_debit_budget_per_line(seeded: Any) -> None:
    from features.inject import build_inject_blocks
    from shared.tokens import estimate_tokens

    mem = _FakeMem()
    lines = [
        "insight (9 узлов): бэкап, master",
        "insight (7 узлов): postgres, phone",
        "insight (5 узлов): прогулка, парк",
    ]
    costs = [estimate_tokens(line) for line in lines]

    two_fit = await build_inject_blocks(mem, None, "gu", budget=sum(costs[:2]))
    two_insights = [b for b in two_fit if b["kind"] == "insight"]
    assert len(two_insights) == 2, two_fit
    assert [b["content"] for b in two_insights] == lines[:2]

    all_fit = await build_inject_blocks(mem, None, "gu", budget=sum(costs))
    all_insights = [b for b in all_fit if b["kind"] == "insight"]
    assert len(all_insights) == 3, all_fit
    assert [b["content"] for b in all_insights] == lines

    small = await build_inject_blocks(mem, None, "gu", budget=1)
    assert not any(b["kind"] == "insight" for b in small), small


@pytest.fixture
async def ranked(tmp_path) -> AsyncIterator[Any]:
    """Four insights for one scope; member counts 5/3/2/0, node ids reversed.

    Node ids ascend with the counts, so ordering by node_id yields 0/2/3 while
    ordering by member count yields 5/3/2 — the two orders cannot be confused.
    """
    from shared.connection import connection_manager

    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    from shared.migrations import MigrationManager

    await MigrationManager(cm=connection_manager).migrate()
    from graph.epistemic import EpistemicGraph

    g = EpistemicGraph(cm=connection_manager, layer="user")
    for count in (0, 2, 3, 5):
        insight_id = await g.add_node("gu", f"insight ({count} узлов): тема{count}", "insight")
        for _ in range(count):
            member = await g.add_node("gu", f"member {count}", "fact")
            await g.add_edge(insight_id, member, "insight_of")
    yield connection_manager
    connection_manager._conns.clear()


async def test_insight_ranking_is_by_member_count(ranked: Any) -> None:
    """[S5] top-3 идут по числу insight_of, а не по node_id."""
    from features.inject import build_inject_blocks

    blocks = await build_inject_blocks(_FakeMem(), None, "gu", budget=2000)
    insights = [b["content"] for b in blocks if b["kind"] == "insight"]
    assert insights == [
        "insight (5 узлов): тема5",
        "insight (3 узлов): тема3",
        "insight (2 узлов): тема2",
    ], insights
