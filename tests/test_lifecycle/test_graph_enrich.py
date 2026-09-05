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

    # Адаптировано под C3/S6b + C6: dream-фаза добавляет блок статистики (нулевой на noop-слое).
    assert result == {
        "nodes_cleaned": 0,
        "miners": {k: {"edges": 0} for k in result["miners"]},
        "sanitation": {"expired": 0},
        "behavior": {},
        "dream": {"nrem_decayed": 0, "nrem_pruned": 0, "rem_bridged": 0, "insights": 0},
    }


# --- C6: трёхфазный dream — NREM decay → REM bridge → Insight abstractions ---


async def _dream_graph(g):
    """Старое heuristic-ребро (NREM-мишень), изолированный дубль (REM-мишень), сообщество (Insight-мишень)."""
    a = await g.add_node("gu", "кэш redis обслуживает воркеры", "fact")
    b = await g.add_node("gu", "воркеры читают кэш redis", "fact")
    c = await g.add_node("gu", "воркеры выполняют задачи очереди", "fact")
    iso = await g.add_node("gu", "кэш redis обслуживает воркеры проекта", "fact")  # дубль A, без рёбер
    await g.add_edge(a, b, "mentions", 0.6, tags=["heuristic:cofire"])
    await g.add_edge(b, c, "mentions", 0.6, tags=["heuristic:cofire"])
    return {"a": a, "b": b, "c": c, "iso": iso}


async def _age_edge(conn: Any, a: int, b: int, days: int, weight: float | None = None) -> None:
    import time

    sql = "UPDATE epi_edges SET created_at=?"
    params: list[Any] = [time.time() - days * 86400]
    if weight is not None:
        sql += ", weight=?"
        params.append(weight)
    sql += " WHERE source_id=? AND target_id=?"
    params.extend([a, b])
    await conn.execute(sql, params)


@pytest.fixture
def no_miners(monkeypatch):
    """Dream-тесты герметичны: минеры выключены (их рёбра и ингибиция шумят)."""
    monkeypatch.setattr("lifecycle.graph_miners.MINERS", {})


@pytest.mark.asyncio
async def test_dream_nrem_decays_and_prunes_weak_edges(graph, no_miners):
    ids = await _dream_graph(graph)
    conn = await connection_manager.get("memory.db")
    # a-b: старое слабое (0.05 → 0.04) → prune; b-c: старое крепкое → −0.01; свежее ребро d-e → +0.05
    d = await graph.add_node("gu", "деплой прошёл без инцидентов", "fact")
    e = await graph.add_node("gu", "мониторинг не заметил деградацию", "fact")
    await graph.add_edge(d, e, "mentions", 0.6, tags=["heuristic:cofire"])
    await _age_edge(conn, ids["a"], ids["b"], days=40, weight=0.05)
    await _age_edge(conn, ids["b"], ids["c"], days=40)
    await conn.commit()

    from lifecycle.graph_enrich import graph_enrich

    result = await graph_enrich(layer="user")
    dream = result["dream"]

    rows = await (await conn.execute("SELECT source_id, target_id, weight FROM epi_edges")).fetchall()
    weights = {(r["source_id"], r["target_id"]): float(r["weight"]) for r in rows}
    assert (ids["a"], ids["b"]) not in weights, "ослабленное ребро (0.05−0.01 < 0.05) удалено NREM"
    assert weights[(ids["b"], ids["c"])] == pytest.approx(0.59), f"неактивное ребро ослаблено на 0.01, {weights}"
    assert weights[(d, e)] == pytest.approx(0.65), f"свежее со-сработавшее ребро усилено на +0.05, {weights}"
    assert dream["nrem_decayed"] >= 2 and dream["nrem_pruned"] >= 1, f"NREM-статистика, dream={dream}"


@pytest.mark.asyncio
async def test_dream_rem_bridges_isolated_duplicates(graph, no_miners):
    ids = await _dream_graph(graph)
    conn = await connection_manager.get("memory.db")

    from lifecycle.graph_enrich import graph_enrich

    result = await graph_enrich(layer="user")
    dream = result["dream"]

    rows = await (await conn.execute("SELECT source_id, target_id, relation, weight FROM epi_edges")).fetchall()
    bridges = [r for r in rows if r["relation"] == "dream_bridge"]
    assert dream["rem_bridged"] >= 1, f"REM должен навести мост к изолированному дублю, dream={dream}"
    assert bridges, "dream_bridge-рёбра материализованы"
    touching = [r for r in bridges if ids["iso"] in (r["source_id"], r["target_id"])]
    assert touching, f"мост идёт к изолированному узлу, bridges={bridges}"
    assert all(0 < float(r["weight"]) <= 0.3 for r in touching), f"weight = sim × 0.3 ≤ 0.3, {touching}"


@pytest.mark.asyncio
async def test_dream_insight_materializes_abstraction(graph, no_miners):
    ids = await _dream_graph(graph)
    conn = await connection_manager.get("memory.db")

    from lifecycle.graph_enrich import graph_enrich

    result = await graph_enrich(layer="user")
    dream = result["dream"]

    rows = await (await conn.execute("SELECT content FROM epi_nodes WHERE node_type='insight'")).fetchall()
    assert dream["insights"] >= 1, f"Insight-фаза материализует абстракцию, dream={dream}"
    assert rows, "узел типа 'insight' создан"
    joined = " ".join(r["content"] for r in rows)
    assert "redis" in joined.lower(), f"insight-узел обобщает члены сообщества, rows={rows!r}"
