"""Phase G Task 7: retrieval ablation arms — rrf | dense_per_kind | gated | full.

Arms для №11-eval сравнения стратегий retrieval:
- 'rrf'            — статус-кво: один 5-source RRF-поиск без EDM/ITS и роутинга;
- 'dense_per_kind' — ENGRAM-упрощение: один поиск per memory-kind, set-merge без RRF-фьюжена;
- 'gated'          — Adaptive RAG: query-features решают, какие источники фаерить;
- 'full'           — dual-route + EDM/ITS (дефолт, не меняет существующее поведение).
"""

from typing import Any

import pytest

from rag.multi_source import MultiSourceRAG
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
    from rag.schema import init_rag_db

    await init_rag_db(connection_manager, True)
    yield connection_manager
    connection_manager._conns.clear()
    connection_manager.base_dir = original  # восстановить: иначе stale tmp-dir травит последующие no-db тесты


async def _fact(key: str, value: str, kind: str = "fact", user_id: str = "ab", importance: float = 0.8) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO core_memory (user_id, layer, key, value, importance, created_at, updated_at, memory_kind) VALUES (?, 'user', ?, ?, ?, ?, ?, ?)",
        (user_id, key, value, importance, T, T, kind),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _page(title: str, content: str, kind: str | None, user_id: str = "ab") -> int:
    """rag-страница с одним чанком; bin_embedding — с query-префиксом (сторона search_binary)."""
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO rag_pages (layer, user_id, title, content, wiki_type) VALUES ('user', ?, ?, ?, 'note')",
        (user_id, title, content),
    )
    page_id = int(cur.lastrowid or 0)
    from rag.quantize import embed_to_binary
    from shared.embeddings import embed_text

    emb = await embed_text(content, prefix="query: ")
    await conn.execute(
        "INSERT INTO rag_chunks (page_id, chunk_index, content, bin_embedding, memory_kind) VALUES (?, 0, ?, ?, ?)",
        (page_id, content, embed_to_binary(emb, threshold=0.0, dim=len(emb)), kind),
    )
    await conn.commit()
    return page_id


def _cand(cid: int, content: str, score: float) -> dict[str, Any]:
    return {"id": cid, "title": f"hit-{cid}", "content": content, "score": score, "source": "fts5"}


# --- retrieval_mode: env → config → default full ---


def test_default_mode_full(monkeypatch) -> None:
    from rag.ablation import retrieval_mode

    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    assert retrieval_mode() == "full"


def test_env_override_and_invalid_fallback(monkeypatch) -> None:
    from rag.ablation import retrieval_mode

    monkeypatch.setenv("RETRIEVAL_MODE", "dense_per_kind")
    assert retrieval_mode() == "dense_per_kind"
    monkeypatch.setenv("RETRIEVAL_MODE", "bogus")
    assert retrieval_mode() == "full", "неизвестное env-значение — деградация к дефолту"


def test_config_mode(monkeypatch) -> None:
    from config import config

    from rag.ablation import retrieval_mode

    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    monkeypatch.setattr(config, "_data", {"retrieval": {"mode": "gated"}}, raising=False)
    assert retrieval_mode() == "gated"


# --- dense_per_kind arm ---


async def test_dense_per_kind_routes_by_kind_for_text(db) -> None:
    from rag.ablation import dense_per_kind_search

    await _fact("f1", "postgres backup cron настроен", kind="fact")
    await _fact("d1", "решил выбрать postgres для бэкапа", kind="decision")

    out = await dense_per_kind_search(connection_manager, "решил postgres для бэкапа", user_id="ab", limit=5)
    assert out, "kind_for_text → decision: должен найтись decision-факт"
    assert all(h["memory_kind"] == "decision" for h in out), f"чужой kind не возвращается, out={out!r}"
    assert any("выбрать" in h["content"] for h in out)
    assert not any(h["title"] == "f1" for h in out)


async def test_dense_per_kind_multiple_kinds_set_merge(db) -> None:
    from rag.ablation import dense_per_kind_search

    await _fact("pref1", "предпочитаю тёмную тему", kind="preference")
    await _fact("obs1", "заметил медленный кэш", kind="observation")
    await _page("page-dec", "деплой ночью прошёл успешно", "decision")

    out = await dense_per_kind_search(
        connection_manager,
        "деплой кэш тему",
        user_id="ab",
        kinds=["preference", "observation", "decision"],
        limit=10,
    )
    kinds_seen = {h["memory_kind"] for h in out}
    assert kinds_seen == {"preference", "observation", "decision"}, f"все kinds слились set-merge'ом, out={out!r}"
    assert all(h["source"] in {"core_kind", "fts5", "mib"} for h in out)
    assert all(h["kind"] == "relevant" for h in out)


async def test_dense_per_kind_rag_fts5_hamming_kind_scoped(db) -> None:
    from rag.ablation import dense_per_kind_search

    await _page("backup-guide", "postgres backup настройка cron", "fact")
    await _page("deploy-dec", "postgres backup настройка деплоя", "decision")
    await _page("borscht", "рецепт борща со свёклой", "decision")

    out = await dense_per_kind_search(connection_manager, "postgres backup настройка", user_id="ab", kinds=["fact"], limit=10)
    titles = {h["title"] for h in out}
    assert titles == {"backup-guide"}, f"FTS5+Hamming скоупятся по kind: чужой kind исключён, titles={titles}"
    bg = [h for h in out if h["title"] == "backup-guide"]
    assert len(bg) == 1, f"set-merge: страница найдена FTS5 и Hamming → без дублей, out={out!r}"


# --- gated arm: query-features + матрица источников ---


def test_query_features() -> None:
    from rag.ablation import query_features

    f = query_features("list all diary entries")
    assert f["is_enumerative"] and f["length"] == 4 and not f["has_entity"] and not f["is_question"]

    f2 = query_features("как настроить postgres?")
    assert f2["is_question"] and f2["has_entity"] and not f2["is_enumerative"]

    f3 = query_features("погода сегодня")
    assert f3["length"] == 2 and not f3["is_question"] and not f3["is_enumerative"] and not f3["has_entity"]


def test_gate_sources_matrix() -> None:
    from rag.ablation import gate_sources, query_features

    assert gate_sources(query_features("list all diary entries")) == {
        "rag": False,
        "wiki": True,
        "episodic": False,
        "core": True,
        "graph": True,
    }, "enumerative: dense-rag выключен, каталог и typed-хранилища включены"
    assert gate_sources(query_features("погода сегодня")) == {
        "rag": True,
        "wiki": False,
        "episodic": False,
        "core": True,
        "graph": False,
    }, "короткий непросительный — быстрый путь rag+core"
    assert gate_sources(query_features("как настроить postgres?"))["graph"] is True, "entity-имя подключает граф"
    long_q = "почему сборка падает из-за кэша и что привело к сбою релиза"
    assert all(gate_sources(query_features(long_q)).values()), "длинный/вопросный — полный fan-out"


async def test_gated_search_fires_flags() -> None:
    from rag.ablation import gated_search

    class RecordingRAG:
        def __init__(self) -> None:
            self.kw: list[dict[str, Any]] = []

        async def search(self, query: str, user_id: str = "default", limit: int = 10, **kw: Any) -> list[dict[str, Any]]:
            self.kw.append(kw)
            return [{"id": 1, "title": "t", "content": "c", "score": 1.0, "source": "core"}]

    rag = RecordingRAG()
    out = await gated_search(rag, "погода сегодня", user_id="u1", limit=3)
    assert rag.kw == [{"include_rag": True, "include_wiki": False, "include_episodic": False, "include_core": True, "include_graph": False}], (
        f"матрица фич → include_* флаги, kw={rag.kw!r}"
    )
    assert out[0]["kind"] == "core"


# --- route_query dispatch: arms переключаются, дефолт full не меняет поведение ---


class FakeRoutedRAG:
    def __init__(self, pool: list[dict[str, Any]]):
        self.pool = pool
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, user_id: str = "default", limit: int = 10, include_graph: bool = True, **kw: Any) -> list[dict[str, Any]]:
        self.calls.append({"include_graph": include_graph, "limit": limit, **kw})
        return [dict(c) for c in self.pool]


async def test_route_query_rrf_mode_plain_search(monkeypatch) -> None:
    from rag.dual_route import route_query

    monkeypatch.setenv("RETRIEVAL_MODE", "rrf")
    rag = FakeRoutedRAG([_cand(1, "как настроить бэкап postgres cron", 0.9), _cand(2, "weather report today", 0.2)])
    out = await route_query(rag, "как настроить бэкап postgres", user_id="u1", limit=5)
    assert rag.calls == [{"include_graph": True, "limit": 5}], "статус-кво: один 5-source RRF без EDM/роутинга"
    assert out[0]["id"] == 1 and out[0]["score"] == 0.9, "scores не переранжированы"


async def test_route_query_gated_mode(monkeypatch) -> None:
    from rag.dual_route import route_query

    monkeypatch.setenv("RETRIEVAL_MODE", "gated")
    rag = FakeRoutedRAG([_cand(1, "diary entry", 0.9)])
    await route_query(rag, "list all diary entries", user_id="u1", limit=5)
    assert rag.calls[0]["include_rag"] is False, "gated: enumerative-маркер выключает dense-rag"
    assert rag.calls[0]["include_wiki"] is True


async def test_route_query_dense_per_kind_mode(monkeypatch, db) -> None:
    from rag.dual_route import route_query

    monkeypatch.setenv("RETRIEVAL_MODE", "dense_per_kind")
    await _fact("d1", "решил выбрать postgres", kind="decision")
    multi = MultiSourceRAG(rag=None, wiki=None, cm=connection_manager)
    out = await route_query(multi, "решил выбрать postgres", user_id="ab", limit=5)
    assert out and all(h["memory_kind"] == "decision" for h in out), f"dense_per_kind через route_query, out={out!r}"


async def test_route_query_default_full_unchanged(monkeypatch) -> None:
    from rag.dual_route import route_query

    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    rag = FakeRoutedRAG([_cand(1, "как настроить бэкап postgres cron", 0.9), _cand(2, "weather report today", 0.2)])
    out = await route_query(rag, "как настроить бэкап postgres", user_id="u1", limit=5)
    assert rag.calls == [{"include_graph": False, "limit": 100}], "дефолт full: текущий dual-route (graph-expand OFF)"
    assert out[0]["id"] == 1
    assert 2 not in [c["id"] for c in out], "чужой скоуп отрезан EDM/ITS как раньше"
