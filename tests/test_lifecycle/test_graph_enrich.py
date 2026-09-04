"""Phase G Task 1: graph_enrich — пре-чистка JSON-мусора + скелет минеров."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager

JUNK = [
    '{"type": "tool_result", "tool_use_id": "t1", "content": "raw tool output"}',
    "деплой упал: tool_use_id=abc-123, повторить шаг 3",
    "[ariel recall] дамп результатов поиска по запросу «память»",
]
CLEAN = [
    "Борис работает в Google",
    "Лили предпочитает кофе без сахара",
    "прод-сервер крутится на VPS в Германии",
    "для Python-проектов используется uv вместо pip",
    "Hermes — агент-обёртка над ariel",
]


@pytest.fixture
async def graph(tmp_path) -> AsyncIterator[Any]:
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()

    from graph.epistemic import EpistemicGraph

    yield EpistemicGraph(cm=connection_manager, layer="user")
    connection_manager._conns.clear()


async def _seed(graph: Any) -> dict[str, int]:
    """5 чистых fact-узлов + 3 JSON-мусорных + одно ребро в мусор."""
    ids = {"clean": {}, "junk": {}}
    for i, text in enumerate(CLEAN):
        ids["clean"][i] = await graph.add_node("gu", text, "fact")
    for i, text in enumerate(JUNK):
        ids["junk"][i] = await graph.add_node("gu", text, "fact")
    await graph.add_edge(ids["junk"][0], ids["clean"][0], "mentions")
    return ids


@pytest.mark.asyncio
async def test_graph_enrich_captures_junk_to_l0_and_cleans_nodes(graph):
    ids = await _seed(graph)
    from lifecycle.graph_enrich import graph_enrich

    result = await graph_enrich(layer="user")

    assert result["nodes_cleaned"] == 3
    conn = await connection_manager.get("memory.db")

    # мусорные узлы удалены, чистые остались
    rows = await (await conn.execute("SELECT node_id, content FROM epi_nodes")).fetchall()
    remaining = {r["node_id"] for r in rows}
    assert len(remaining) == 5
    assert all(nid in remaining for nid in ids["clean"].values())
    assert all(nid not in remaining for nid in ids["junk"].values())

    # мусор захвачен в L0 (восстановимо), по одному capture на узел
    l0 = await (await conn.execute("SELECT text FROM l0_journal WHERE event='graph_cleanup' ORDER BY id")).fetchall()
    assert [r["text"] for r in l0] == JUNK

    # каскад: ребро мусор→чистое удалено, новых рёбер минеры не навели
    edges = await (await conn.execute("SELECT COUNT(*) FROM epi_edges")).fetchone()
    assert edges[0] == 0


@pytest.mark.asyncio
async def test_graph_enrich_miner_stubs_report_zero_edges(graph):
    from lifecycle.graph_enrich import graph_enrich

    result = await graph_enrich(layer="user")

    assert result["miners"], "скелет минеров пуст"
    assert all(v == {"edges": 0} for v in result["miners"].values())


@pytest.mark.asyncio
async def test_graph_enrich_noop_layer_keeps_stats_shape(graph):
    from lifecycle.graph_enrich import graph_enrich

    result = await graph_enrich(layer="agent")

    assert result == {"nodes_cleaned": 0, "miners": {k: {"edges": 0} for k in result["miners"]}, "sanitation": {"expired": 0}}
