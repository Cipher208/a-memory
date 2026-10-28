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


async def _theme(graph: Any, filler: str, distinct: str | list[str], count: int) -> list[int]:
    """Кластер: несколько узлов с общим словом + различительные слова, плотная связность."""
    words = [distinct] if isinstance(distinct, str) else distinct
    ids = []
    for i in range(count):
        # различительное слово — в ПОЛОВИНЕ узлов (при списке — по паре на слово):
        # по частоте оно уступает общим словам (запись/ещё/заполнитель — в 100%),
        # по различительности ratio=2 оно занимает первые места. Это и есть
        # инвариант S4.
        word = words[min(i // 2, len(words) - 1)]
        verb = f"обсуждает {word}" if i % 2 == 0 else "обсуждает детали"
        text = f"запись {i} {filler} {verb} и ещё {filler}"
        ids.append(await graph.add_node("gu", text, "fact"))
    for left, right in pairwise(ids):
        await graph.add_edge(left, right, "mentions")
    await graph.add_edge(ids[0], ids[-1], "mentions")
    return ids


async def _path_theme(graph: Any, filler: str, distinct: str, count: int) -> list[int]:
    """Тема-цепочка: без плотного ядра, поэтому один тяжёлый мост её сливает.

    Нужна [S6(3)]: на плотном кластере один мост Louvain игнорирует, и тест
    не отличил бы «ребро отфильтровано» от «мост слишком слаб, чтобы слить».
    """
    ids = []
    for i in range(count):
        verb = f"обсуждает {distinct}" if i % 2 == 0 else "обсуждает детали"
        ids.append(await graph.add_node("gu", f"запись {i} {filler} {verb} и ещё {filler}", "fact"))
    for left, right in pairwise(ids):
        await graph.add_edge(left, right, "mentions")
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


async def test_insight_of_and_nonpositive_edges_do_not_fuse_communities(graph: Any, no_miners: None) -> None:
    """[S6(3)] insight_of и weight<=0 — мёртвые рёбра: они не сливают сообщества."""
    a = await _path_theme(graph, "бэкап", "postgres", 3)
    b = await _path_theme(graph, "прогулка", "парк", 3)
    # Натуральный insight_of всегда имеет insight на одном конце, и снимок
    # удаляет такие рёбра до чтения входа. Здесь ребро между двумя НЕ-insight
    # узлами создаётся напрямую: только так фильтр входа вообще проверяется.
    await graph.add_edge(a[1], b[1], "insight_of", weight=3.0)
    await graph.add_edge(a[0], b[0], "mentions", weight=-50.0)
    contents = await _pass()
    assert len(contents) == 2, contents
    joined = " | ".join(contents)
    assert "postgres" in joined and "парк" in joined
    assert "бэкап" in joined and "прогулка" in joined


async def test_tokens_are_distinguishing_not_frequent(graph: Any, no_miners: None) -> None:
    """[S6(2)] Общий токен не попадает в топ-3; различительные — попадают."""
    from rag.edm import tokens

    a_words = ["уникальноесловоальфа", "уникальноесловобета", "уникальноесловогамма", "уникальноесловодельта"]
    b_words = ["уникальноесловое", "уникальноесловожэ", "уникальноесловозий", "уникальноесловоий"]
    await _theme(graph, "общиймаркер", a_words, 8)
    await _theme(graph, "общиймаркер", b_words, 8)
    contents = await _pass()
    assert len(contents) == 2, contents
    # Общий маркер встречается в 100% узлов (ratio=1) и в каждом сообществе
    # проигрывает любому различительному слову (ratio=2): в топ-3 он не входит.
    per_insight = [tokens(c) for c in contents]
    assert all("общиймаркер" not in t for t in per_insight), contents
    # Топ-3 — три из четырёх различительных слов каждой темы (четвёртому
    # слотов нет), т.е. все три слота заняты различительными, а не частотными.
    assert any(len(t & set(a_words)) >= 3 for t in per_insight), contents
    assert any(len(t & set(b_words)) >= 3 for t in per_insight), contents


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
