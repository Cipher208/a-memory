"""Spec [S3]-[S4]: insight = top-10 Louvain-сообществ, токены различительные."""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Any

import pytest
from shared.connection import connection_manager
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def graph(tmp_path) -> AsyncIterator[Any]:
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    from graph.epistemic import EpistemicGraph

    yield EpistemicGraph(cm=connection_manager, layer="user")
    connection_manager._conns.clear()


@pytest.fixture
def no_miners(monkeypatch):
    monkeypatch.setattr("lifecycle.graph_miners.MINERS", {})


async def _theme(graph: Any, filler: str, distinct: str, count: int) -> list[int]:
    """Кластер: несколько узлов с общим словом + различительное слово, плотная связность."""
    ids = []
    for i in range(count):
        # distinct в ПОЛОВИНЕ узлов: по частоте он уступает общим словам
        # (запись/ещё/заполнитель — в 100%), по различительности ratio=2 он
        # занимает первое место. Это и есть инвариант S4.
        verb = f"обсуждает {distinct}" if i % 2 == 0 else "обсуждает детали"
        text = f"запись {i} {filler} {verb} и ещё {filler}"
        ids.append(await graph.add_node("gu", text, "fact"))
    for left, right in pairwise(ids):
        await graph.add_edge(left, right, "mentions")
    await graph.add_edge(ids[0], ids[-1], "mentions")
    return ids


async def _pass() -> list[str]:
    from lifecycle.graph_enrich import graph_enrich

    await graph_enrich(layer="user")
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    rows = await (await conn.execute("SELECT content FROM epi_nodes WHERE node_type='insight'")).fetchall()
    return [str(r["content"]) for r in rows]


async def test_communities_split_two_themes(graph: Any, no_miners: None) -> None:
    """Две плотно связанных темы → два insight, токены каждого свои."""
    a = await _theme(graph, "бэкап", "postgres", 6)
    b = await _theme(graph, "прогулка", "парк", 6)
    # Слабый мост: компонента одна — расщепить её обязан Louvain, не BFS.
    await graph.add_edge(a[-1], b[0], "mentions")
    contents = await _pass()
    assert len(contents) == 2, contents
    joined = " | ".join(contents)
    assert "postgres" in joined and "парк" in joined
    assert "бэкап" in joined and "прогулка" in joined


async def test_tokens_are_distinguishing_not_frequent(graph: Any, no_miners: None) -> None:
    """Общий токен не попадает в топ-3; различающий — попадает."""
    await _theme(graph, "общиймаркер", "уникальноесловода", 8)
    await _theme(graph, "общиймаркер", "уникальноесловоб", 8)
    contents = await _pass()
    joined = " | ".join(contents)
    assert "уникальноесловода" in joined and "уникальноесловоб" in joined
    # общеграфовой маркер встречается в 100% узлов — по частоте он занимает
    # топ в ОБОИХ сообществах; по различительности ratio=2 он не выбивает
    # различительные слова из топ-3
    assert joined.count("общиймаркер") <= 3


async def test_second_pass_recomputes_communities_without_history(graph: Any, no_miners: None) -> None:
    """Повторный проход пересчитывает текущие сообщества, а не историю снимка."""
    await _theme(graph, "темаодин", "словоодин", 6)
    await _theme(graph, "темадва", "словодва", 6)
    first = await _pass()
    assert len(first) == 2, first
    # второй проход заменяет снимок и снова получает две тематические группы.
    second = await _pass()
    assert len(second) == 2, second
    assert any("словоодин" in c for c in second)
