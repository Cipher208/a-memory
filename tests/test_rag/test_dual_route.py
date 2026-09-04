"""Phase G Task 6: dual-route retrieval — EDM/ITS re-rank, S2 exhaustive, D-Mem escalation.

RRF остаётся recall-first генератором; EDM переранживает top-100:
EDM(m|q,S) = α·R + β·N + γ·G − δ·K, итог min-max → [0,1], ITS threshold 0.05.
"""

from typing import Any

import pytest

from rag.edm import ITS_THRESHOLD, edm_rerank, inhibit_scores, minmax
from rag.multi_source import _ID_OFFSET_GRAPH
from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager

T = 1_700_000_000.0


@pytest.fixture
async def db(tmp_path):
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()
    connection_manager.base_dir = original  # восстановить: иначе stale tmp-dir травит последующие no-db тесты


async def _node(content: str, node_type: str = "fact", user_id: str = "gu") -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user', ?, ?, ?, '[]', 0.5, ?)",
        (user_id, content, node_type, T),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _led_edge(a: int, b: int) -> None:
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, 'led_to', 0.3, ?, ?)",
        (a, b, T, '["heuristic:marker"]'),
    )
    await conn.commit()


def _gnode(node_id: int, content: str, score: float) -> dict[str, Any]:
    return {
        "id": -node_id - _ID_OFFSET_GRAPH,
        "title": f"Graph Node {node_id} (fact)",
        "content": content,
        "score": score,
        "source": "graph",
    }


def _cand(cid: int, content: str, score: float, title: str = "hit") -> dict[str, Any]:
    return {"id": cid, "title": title, "content": content, "score": score, "source": "fts5"}


# --- classify_query (question-type router) ---


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("как настроить бэкап postgres", "factual"),
        ("починила кэш, теперь работает?", "factual"),
        ("перечисли все правила кэша", "enumerative"),
        ("list all diary entries", "enumerative"),
        ("список всех решений", "enumerative"),
        ("почему сборка падает из-за кэша", "multi-hop"),
        ("как это влияет на следующий релиз и что привело к сбою", "multi-hop"),
        ("word " + " ".join(f"w{i}" for i in range(12)), "multi-hop"),  # длина
    ],
)
def test_classify_query(query: str, expected: str) -> None:
    from rag.dual_route import classify_query

    assert classify_query(query) == expected


# --- lateral inhibition pre-step (SYNAPSE, in-memory) ---


def test_inhibit_scores_suppresses_weak_by_strong_cluster() -> None:
    scores = [0.6] * 7 + [0.3]
    out = inhibit_scores(scores)
    assert out[7] == pytest.approx(0.0)  # 0.3 − 0.15·7·0.3 < 0 → гасится
    assert out[0] == pytest.approx(0.6)  # равные сильные друг друга не давят


def test_inhibit_scores_no_stronger_neighbours_unchanged() -> None:
    assert inhibit_scores([0.9]) == [0.9]
    assert inhibit_scores([]) == []


def test_minmax_degenerate_keeps_all() -> None:
    assert minmax([0.5, 0.5, 0.5]) == [1.0, 1.0, 1.0]
    assert minmax([]) == []
    assert minmax([2.0, 1.0]) == [1.0, 0.0]


# --- EDM: K-член (semantic dedup) понижает дубликат ---


async def test_edm_lowers_duplicate() -> None:
    query = "настройка кэша postgres"
    dup = [
        _cand(1, "настройка кэша postgres через redis", 0.9, "hit-a"),
        _cand(2, "настройка кэша postgres через redis", 0.9, "hit-dup"),  # тот же текст
        _cand(3, "git rebase workflow tips", 0.9, "hit-noise"),
    ]
    out = await edm_rerank(dup, query)
    ids = [c["id"] for c in out]
    assert 1 in ids
    assert 2 not in ids, f"дубликат должен быть отрезан K-членом, ids={ids}"


async def test_edm_boosts_replenishing_block() -> None:
    # A покрывает postgres+backup; C добавляет новый токен запроса (настройка) — восполняющий.
    query = "postgres backup настройка"
    cands = [
        _cand(1, "postgres backup cron", 0.9, "hit-a"),
        _cand(2, "weather report today", 0.9, "hit-noise"),
        _cand(3, "настройка redis sentinel", 0.9, "hit-c"),
    ]
    out = await edm_rerank(cands, query)
    ids = [c["id"] for c in out]
    assert 3 in ids, f"восполняющий блок (новый токен запроса) должен пройти, ids={ids}"
    assert ids[0] == 1, f"топ-покрытие остаётся первым, ids={ids}"
    if 2 in ids:  # шум может быть отрезан ITS целиком — тогда порядок тривиален
        assert ids.index(3) < ids.index(2), f"восполняющий должен опережать шум, ids={ids}"


# --- EDM: G-член (led_to-завершение цепочки) ---


async def test_edm_chain_completer_boosted(db) -> None:
    a = await _node("починила кэш — перезапуск воркера")
    b = await _node("теперь работает кэш без ошибок")
    c = await _node("рецепт борща со свёклой")
    await _led_edge(a, b)

    cands = [
        _gnode(a, "починила кэш — перезапуск воркера", 0.9),
        _gnode(b, "теперь работает кэш без ошибок", 0.5),
        _gnode(c, "рецепт борща со свёклой", 0.55),
    ]
    out = await edm_rerank([dict(x) for x in cands], "починила кэш", cm=connection_manager, user_id="gu")
    ids = [c["id"] for c in out]
    assert -b - _ID_OFFSET_GRAPH in ids, f"led_to-продолжение должно пройти гейт, ids={ids}"
    assert ids.index(-b - _ID_OFFSET_GRAPH) < ids.index(-c - _ID_OFFSET_GRAPH), f"led_to-продолжение должно опережать неродственный узел, ids={ids}"

    # контроль: без led_to-ребра продолжение не получает G-бонус — b не обгоняет c
    out2 = await edm_rerank([dict(x) for x in cands], "починила кэш")
    ids2 = [c["id"] for c in out2]
    assert -b - _ID_OFFSET_GRAPH not in ids2 or ids2.index(-b - _ID_OFFSET_GRAPH) > ids2.index(-c - _ID_OFFSET_GRAPH), (
        f"без ребра G=0: b не должен обгонять c, ids={ids2}"
    )


# --- ITS gating ---


async def test_its_gating_trims_garbage() -> None:
    query = "postgres backup настройка"
    cands = [
        _cand(1, "postgres backup настройка cron", 0.9, "hit-a"),
        _cand(2, "weather report today", 0.1, "hit-noise"),
        _cand(3, "cat video compilation", 0.05, "hit-junk"),
    ]
    out = await edm_rerank(cands, query)
    ids = [c["id"] for c in out]
    assert 1 in ids
    assert 2 not in ids and 3 not in ids, f"мусор ниже ITS-порога отрезан, ids={ids}"
    assert all(0.0 <= c["score"] <= 1.0 for c in out)
    assert out[0]["score"] >= ITS_THRESHOLD


async def test_its_cap_respects_limit() -> None:
    cands = [_cand(i, f"postgres backup настройка part {i}", 0.9 - i * 0.05, f"hit-{i}") for i in range(30)]
    out = await edm_rerank(cands, "postgres backup настройка", k_cap=5)
    assert len(out) <= 5


# --- S2 exhaustive route ---


class FakeWiki:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    async def list_all(self, limit: int = 50, status: str | None = "active") -> list[dict[str, Any]]:
        return self._rows[:limit]


async def test_s2_route_list_all_wiki() -> None:
    from rag.dual_route import s2_exhaustive

    rows = [{"entry_id": i, "title": f"diary-{i}", "content": f"запись {i}", "wiki_type": "diary"} for i in range(6)]
    rows += [{"entry_id": 100, "title": "note", "content": "заметка", "wiki_type": "notes"}]
    out = await s2_exhaustive(FakeWiki(rows), None, "list all diary entries", user_id="u1")
    assert len(out) == 6, "полный сбор детей категории — без top-k"
    assert all(r["source"] == "s2_exhaustive" for r in out)
    assert all("diary" in str(r["wiki_type"]) for r in out)


async def test_s2_route_graph_node_type(db) -> None:
    from rag.dual_route import s2_exhaustive

    for i in range(4):
        await _node(f"решение {i} по кэшу", node_type="decision_log")
    await _node("обычный факт", node_type="fact")

    out = await s2_exhaustive(None, connection_manager, "list all decision_log", user_id="gu")
    assert len(out) == 4, "полный спуск детей типа узла"
    assert all(r["source"] == "s2_exhaustive" for r in out)


# --- D-Mem escalation + router dispatch ---


class FakeRoutedRAG:
    def __init__(self, dense_pool: list[dict[str, Any]], graph_pool: list[dict[str, Any]] | None = None, cm: Any = None, wiki: Any = None):
        self.pool = dense_pool
        self.graph_pool = graph_pool if graph_pool is not None else dense_pool
        self.cm = cm
        self.wiki = wiki
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, user_id: str = "default", limit: int = 10, include_graph: bool = True, **kw: Any) -> list[dict[str, Any]]:
        self.calls.append({"include_graph": include_graph, "limit": limit})
        return [dict(c) for c in (self.graph_pool if include_graph else self.pool)]


async def test_route_factual_no_graph_expand() -> None:
    from rag.dual_route import route_query

    rag = FakeRoutedRAG([_cand(1, "как настроить бэкап postgres cron", 0.9), _cand(2, "weather report today", 0.2)])
    out = await route_query(rag, "как настроить бэкап postgres", user_id="u1", limit=5)
    assert rag.calls == [{"include_graph": False, "limit": 100}]  # graph-expand OFF на single-hop factual
    assert out[0]["id"] == 1
    assert 2 not in [c["id"] for c in out]  # чужой скоуп отрезан EDM/ITS


async def test_route_dmem_escalation_on_low_dense_confidence() -> None:
    from rag.dual_route import route_query

    graph_pool = [
        _cand(1, "почему деплой упал после релиза цепочка событий", 0.2),
        _gnode(7, "деплой упал: переполнение диска на релизе", 0.5),
    ]
    rag = FakeRoutedRAG(
        [_cand(1, "weather report today", 0.2)],  # dense-пул не покрывает запрос
        graph_pool=graph_pool,
    )
    out = await route_query(rag, "почему деплой упал после релиза", user_id="u1", limit=5)
    assert [c["include_graph"] for c in rag.calls] == [False, True], "escalation: dense слабый → graph-rerank"
    assert any(c["id"] == -7 - _ID_OFFSET_GRAPH for c in out), "graph-узел подтянут эскалацией"


async def test_route_no_escalation_when_dense_confident() -> None:
    from rag.dual_route import route_query

    rag = FakeRoutedRAG([_cand(1, "почему деплой упал после релиза цепочка событий", 0.9)])
    await route_query(rag, "почему деплой упал после релиза", user_id="u1", limit=5)
    assert len(rag.calls) == 1, "уверенный dense не эскалируется"


async def test_route_enumerative_uses_s2_without_dense_search() -> None:
    from rag.dual_route import route_query

    rows = [{"entry_id": i, "title": f"diary-{i}", "content": f"запись {i}", "wiki_type": "diary"} for i in range(6)]
    rag = FakeRoutedRAG([_cand(1, "что-то", 0.9)], wiki=FakeWiki(rows))
    out = await route_query(rag, "list all diary entries", user_id="u1", limit=3)
    assert rag.calls == [], "S2-маршрут не ходит в dense-pool"
    assert len(out) == 6, "exhaustive: без top-k"


# --- precision (Tenure-стиль): не вернуть чужой скоуп ---


async def test_precision_tenure_style_scope_isolation(db) -> None:
    from core.memory import CoreMemory
    from rag.dual_route import route_query
    from rag.multi_source import MultiSourceRAG

    core = CoreMemory(cm=connection_manager)
    await core._init_db()
    await core.save("u1", "tenure_alpha_report", "проект альфа сдаёт отчёт по пятницам", importance=0.9)
    await core.save("u1", "beta_office", "команда бета переехала в новый офис на кузнецком", importance=0.9)

    multi = MultiSourceRAG(rag=None, wiki=None, cm=connection_manager)
    out = await route_query(multi, "когда проект альфа сдаёт отчёт", user_id="u1", limit=5)
    contents = " | ".join(str(c.get("content") or "") for c in out)
    assert "альфа" in contents, f"свой скоуп должен вернуться, out={out!r}"
    assert out and out[0]["score"] == pytest.approx(max(c["score"] for c in out)), f"свой скоуп — топ-1, out={out!r}"
    assert "бета" not in contents, f"чужой скоуп не должен попасть в выдачу, out={out!r}"


# --- per-kind caps + precedence rules инъекции (отложено из F) ---


class _FakeRagHits:
    def __init__(self, n: int):
        self._n = n

    async def search(self, query: str, user_id: str = "default", limit: int = 10, **kw: Any) -> list[dict[str, Any]]:
        return [{"content": f"relevant hit {i}", "score": 0.9 - i * 0.1} for i in range(self._n)]


class _FakeMem:
    def __init__(self, *, with_recent: bool = False, with_important: bool = False) -> None:
        import time
        from types import SimpleNamespace

        recent = [SimpleNamespace(role="user", content="привет из сессии", timestamp=time.time())] if with_recent else []
        facts = [SimpleNamespace(key="crit", value="критичный важный факт проекта", importance=0.9)] if with_important else []
        self.l1 = SimpleNamespace(get_recent=lambda n: recent)
        self.l3 = SimpleNamespace(search_by_tag=self._empty)
        self._facts = facts
        self.l4 = SimpleNamespace(get_all=self._l4)

    async def _empty(self, user_id: str, tag: str, limit: int = 5) -> list[Any]:
        return []

    async def _l4(self, user_id: str, limit: int = 50) -> list[Any]:
        return self._facts


async def test_inject_kind_caps(monkeypatch) -> None:
    from features.inject import build_inject_blocks

    big = 10000
    blocks = await build_inject_blocks(_FakeMem(), _FakeRagHits(5), "u1", text="поиск", budget=big)
    assert len([b for b in blocks if b["kind"] == "relevant"]) == 5  # дефолт: без капов

    from config import config

    monkeypatch.setattr(config, "_data", {"inject": {"kind_caps": {"relevant": 2}}}, raising=False)
    try:
        blocks = await build_inject_blocks(_FakeMem(), _FakeRagHits(5), "u1", text="поиск", budget=big)
        assert len([b for b in blocks if b["kind"] == "relevant"]) == 2
    finally:
        monkeypatch.undo()


async def test_inject_kind_order(monkeypatch) -> None:
    from features.inject import build_inject_blocks

    big = 10000
    from config import config

    monkeypatch.setattr(
        config,
        "_data",
        {"inject": {"kind_order": ["recent", "relevant", "important", "cache_break"]}},
        raising=False,
    )
    try:
        blocks = await build_inject_blocks(_FakeMem(with_recent=True, with_important=True), _FakeRagHits(3), "u1", text="поиск", budget=big)
        kinds = [b["kind"] for b in blocks]
        # стабильные до маркера, динамика после — в заданном kind_order порядке
        assert kinds == ["important", "cache_break", "recent", "relevant", "relevant", "relevant"], f"kinds={kinds}"
    finally:
        monkeypatch.undo()
