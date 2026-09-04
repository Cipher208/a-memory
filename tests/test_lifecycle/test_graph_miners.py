"""Phase G Task 2: минеры #1 tags, #2 token-overlap, #4 sessions.

Фикстура: epi_nodes с тегами (epi_tags), текстами, L0-записями одной сессии.
Каждое ребро минера обязано нести tags LIKE '%heuristic:<name>%'; повторный
вызов не дублирует рёбра (INSERT OR IGNORE по PK epi_edges).
"""

import json
import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager

T = 1_700_000_000.0


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[Any]:
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _node(content: str, ts: float, tags: list[str] | None = None, conf: float = 0.5) -> int:
    """Факт-узел с контролем created_at/confidence + прямая запись в epi_tags."""
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user', 'gu', ?, 'fact', ?, ?, ?)",
        (content, json.dumps(tags or []), conf, ts),
    )
    nid = int(cur.lastrowid or 0)
    for tag in tags or []:
        await conn.execute("INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, ?)", (nid, tag))
    await conn.commit()
    return nid


async def _l0(text: str, ts: float, source_msg_id: int | None = None) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type)"
        " VALUES (?, 'new_message', ?, 'user', 'gu', ?, 'user-message')",
        (ts, source_msg_id, text),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _edges(relation: str) -> list[Any]:
    conn = await connection_manager.get(DB_NAME)
    return await (await conn.execute("SELECT * FROM epi_edges WHERE relation=? ORDER BY source_id, target_id", (relation,))).fetchall()


async def _edge(a: int, b: int, relation: str = "mentions", weight: float = 0.8) -> None:
    """Ручное (не эвристическое) ребро — как их создаёт EpistemicGraph.add_edge."""
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, '[]')",
        (a, b, relation, weight, T),
    )
    await conn.commit()


# --- (a) miner_tags: общий тег → tagged, weight = min(0.3+0.1*shared, 0.6) ---


@pytest.mark.asyncio
async def test_miner_tags_shared_tag_creates_weighted_edge(db):
    n1 = await _node("факт один", T, ["postgres", "deploy"])
    n2 = await _node("факт два", T, ["postgres"])
    n3 = await _node("факт три", T, ["postgres", "deploy", "linux"])
    await _node("факт без тегов", T)  # n4: изолирован — без тегов ребра нет

    from lifecycle.graph_miners import miner_tags

    result = await miner_tags(db, "user")

    assert result["edges"] == 3  # (n1,n2) (n1,n3) (n2,n3); n4 изолирован
    rows = await _edges("tagged")
    assert len(rows) == 3
    by_pair = {(r["source_id"], r["target_id"]): r for r in rows}
    assert by_pair[(min(n1, n3), max(n1, n3))]["weight"] == pytest.approx(0.5)  # 2 общих тега
    # 1 общий тег; G5 lateral inhibition: 0.4 → 0.385 (сосед 0.5 сильнее) → 0.38275 (сосед (2,3) 0.4 > 0.385)
    assert by_pair[(min(n1, n2), max(n1, n2))]["weight"] == pytest.approx(0.38275)
    for r in rows:
        assert "heuristic:tags" in r["tags"]


@pytest.mark.asyncio
async def test_miner_tags_weight_capped_and_no_tag_no_edge(db):
    await _node("a", T, ["x", "y", "z", "w"])
    await _node("b", T, ["x", "y", "z", "w"])
    await _node("c", T, [])
    from lifecycle.graph_miners import miner_tags

    await miner_tags(db, "user")

    rows = await _edges("tagged")
    assert len(rows) == 1
    assert rows[0]["weight"] == pytest.approx(0.6)  # 0.3 + 0.1*4 → cap 0.6


# --- (b) miner_tokens: ≥2 общих редких токена → topic_overlap, weight=Jaccard ---


@pytest.mark.asyncio
async def test_miner_tokens_two_shared_rare_tokens_creates_jaccard_edge(db):
    nA = await _node("миграция postgres wal режима завершена", T)
    nB = await _node("настройка postgres wal режима", T)
    await _node("полностью посторонний текст про ужины", T)
    await _node("postgres обновлён сегодня", T)  # только 1 общий токен с nA → порог не пройден

    from lifecycle.graph_miners import miner_tokens

    result = await miner_tokens(db, "user")

    assert result["edges"] == 1
    rows = await _edges("topic_overlap")
    assert len(rows) == 1
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (min(nA, nB), max(nA, nB))
    # shared={postgres, режима}, union={миграция, postgres, режима, завершена, настройка} → 2/5
    assert rows[0]["weight"] == pytest.approx(0.4)
    assert "heuristic:tokens" in rows[0]["tags"]


@pytest.mark.asyncio
async def test_miner_tokens_below_jaccard_threshold_no_edge(db):
    # shared=1 (<2) и jaccard низкий — ребра нет
    await _node("деплой прошёл успешно ночью", T)
    await _node("деплой на стейджинг", T)
    from lifecycle.graph_miners import miner_tokens

    result = await miner_tokens(db, "user")

    assert result["edges"] == 0


# --- (c) miner_sessions: L0 user-message (source_msg_id / близкие ts) + узлы по ts-окну / синонимам ---


@pytest.mark.asyncio
async def test_miner_sessions_binds_nodes_via_ts_and_synonyms(db):
    # сессия A: две L0-записи с близкими ts (одна с source_msg_id)
    await _l0("решила перейти на PostgreSQL для проекта", T, source_msg_id=101)
    await _l0("настроили backup скрипт вечером", T + 60)
    # сессия B: отдельная L0-запись далеко по времени
    await _l0("деплой прошёл успешно", T + 500_000, source_msg_id=555)

    n1 = await _node("переход на postgres зафиксирован", T + 10)  # ts-окно → A
    n2 = await _node("бэкап настроен и проверен", T + 30)  # ts-окно → A
    # n3: далеко от всех L0 по ts, но «deployment» ≡ «деплой» (синоним-канонизация) → B
    n3 = await _node("deployment прошёл без инцидентов", T + 700)
    n4 = await _node("деплой на проде завершён", T + 500_010)  # ts-окно → B
    await _node("случайная заметка про ужин", T + 250_000)  # ни ts, ни токены → без сессии

    from lifecycle.graph_miners import miner_sessions

    result = await miner_sessions(db, "user")

    assert result["edges"] == 2
    rows = await _edges("same_session")
    assert len(rows) == 2
    pairs = {(r["source_id"], r["target_id"]) for r in rows}
    assert pairs == {(n1, n2), (min(n3, n4), max(n3, n4))}
    for r in rows:
        assert r["weight"] == pytest.approx(0.3)
        assert "heuristic:sessions" in r["tags"]


# --- (f) Task G3: журнал co-retrieval + минеры #5 provenance, #7 co_recalled ---


async def _core_fact(value: str, parents: list[str]) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, source, metadata, created_at, updated_at)"
        " VALUES ('user', 'gu', ?, ?, 0.7, 'episode_promotion', ?, ?, ?)",
        (f"ep_{value[:12]}", value, json.dumps({"parents": parents}), T, T),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


@pytest.mark.asyncio
async def test_log_co_pairs_writes_prefixed_pairs(db):
    from lifecycle.graph_miners import log_co_pairs
    from rag.multi_source import _ID_OFFSET_GRAPH

    n1 = await _node("postgres wal", T)
    n2 = await _node("postgres индексы", T)
    hits = [
        {"id": -n1 - _ID_OFFSET_GRAPH, "source": "graph", "content": "a", "score": 0.5},
        {"id": 77, "source": "fts", "content": "b", "score": 0.9},
        {"id": -n2 - _ID_OFFSET_GRAPH, "source": "graph_expand", "content": "c", "score": 0.4},
        {"content": "хит без id не журналируется", "score": 0.1},
    ]

    written = await log_co_pairs(db, "postgres wal", hits)

    assert written == 3
    conn = await connection_manager.get(DB_NAME)
    rows = await (await conn.execute("SELECT node_a, node_b FROM recall_co_pairs")).fetchall()
    pairs = {(r["node_a"], r["node_b"]) for r in rows}
    a, b = sorted((f"g:{n1}", f"g:{n2}"))
    expected = {tuple(sorted((a, b))), tuple(sorted((a, "f:77"))), tuple(sorted((b, "f:77")))}
    assert pairs == expected


@pytest.mark.asyncio
async def test_log_co_pairs_fewer_than_two_refs_writes_nothing(db):
    from lifecycle.graph_miners import log_co_pairs
    from rag.multi_source import _ID_OFFSET_GRAPH

    hits = [{"id": -5 - _ID_OFFSET_GRAPH, "source": "graph"}]
    assert await log_co_pairs(db, "q", hits) == 0
    conn = await connection_manager.get(DB_NAME)
    assert (await (await conn.execute("SELECT COUNT(*) FROM recall_co_pairs")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_miner_co_retrieval_creates_edge_at_count_two(db):
    from lifecycle.graph_miners import log_co_pairs, miner_co_retrieval
    from rag.multi_source import _ID_OFFSET_GRAPH

    n1 = await _node("совместный хит один", T)
    n2 = await _node("совместный хит два", T)
    hits = [
        {"id": -n1 - _ID_OFFSET_GRAPH, "source": "graph", "content": "a", "score": 0.5},
        {"id": -n2 - _ID_OFFSET_GRAPH, "source": "graph_expand", "content": "b", "score": 0.4},
        {"id": 77, "source": "fts", "content": "c", "score": 0.9},
    ]
    await log_co_pairs(db, "запрос", hits)
    await log_co_pairs(db, "запрос", hits)  # второй совместный recall

    result = await miner_co_retrieval(db, "user")

    assert result["edges"] == 1  # только g:-пара набрала count=2; f:77-пары отфильтрованы
    rows = await _edges("co_recalled")
    assert len(rows) == 1
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (min(n1, n2), max(n1, n2))
    assert rows[0]["weight"] == pytest.approx(0.5)  # 0.3 + 0.1*2
    assert "heuristic:co_retrieval" in rows[0]["tags"]


@pytest.mark.asyncio
async def test_miner_co_retrieval_single_occurrence_no_edge(db):
    from lifecycle.graph_miners import log_co_pairs, miner_co_retrieval
    from rag.multi_source import _ID_OFFSET_GRAPH

    n1 = await _node("одиночный хит", T)
    n2 = await _node("второй одиночный", T)
    hits = [{"id": -n1 - _ID_OFFSET_GRAPH, "source": "graph"}, {"id": -n2 - _ID_OFFSET_GRAPH, "source": "graph"}]
    await log_co_pairs(db, "q", hits)

    assert (await miner_co_retrieval(db, "user"))["edges"] == 0
    assert await _edges("co_recalled") == []


@pytest.mark.asyncio
async def test_miner_provenance_episode_parent_creates_sourced_from(db):
    from lifecycle.graph_miners import miner_provenance

    fact_node = await _node("ключевое решение зафиксировано", T)
    await _core_fact("ключевое решение зафиксировано", ["episode:42"])
    await _core_fact("факт без графового узла — пропущен", ["episode:43"])

    result = await miner_provenance(db, "user")

    assert result["edges"] == 1
    rows = await _edges("sourced_from")
    assert len(rows) == 1
    assert rows[0]["target_id"] == fact_node
    assert rows[0]["weight"] == pytest.approx(0.5)
    assert "heuristic:provenance" in rows[0]["tags"]
    conn = await connection_manager.get(DB_NAME)
    ep = await (await conn.execute("SELECT * FROM epi_nodes WHERE node_id=?", (rows[0]["source_id"],))).fetchone()
    assert ep["node_type"] == "episode"
    assert ep["content"] == "episode:42"  # узел эпизода создан по parents-ссылке


@pytest.mark.asyncio
async def test_miner_provenance_reuses_existing_episode_node_and_no_parents_no_edge(db):
    from lifecycle.graph_miners import miner_provenance

    await _node("вторичное наблюдение", T)
    await _core_fact("вторичное наблюдение", ["episode:9", "event:3"])  # только episode:-ссылки
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
        " VALUES ('user', 'gu', 'episode:9', 'episode', '[]', 0.5, ?)",
        (T,),
    )
    await conn.commit()
    ep_id = int(cur.lastrowid or 0)

    result = await miner_provenance(db, "user")

    assert result["edges"] == 1
    rows = await _edges("sourced_from")
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (ep_id, rows[0]["target_id"])
    dupes = await (await conn.execute("SELECT COUNT(*) FROM epi_nodes WHERE content='episode:9'")).fetchone()
    assert dupes[0] == 1  # find_or_add: существующий узел переиспользован


@pytest.mark.asyncio
async def test_provenance_and_co_retrieval_idempotent(db):
    from lifecycle.graph_miners import log_co_pairs, miner_co_retrieval, miner_provenance
    from rag.multi_source import _ID_OFFSET_GRAPH

    fact_node = await _node("решение про дежурство", T)
    await _core_fact("решение про дежурство", ["episode:7"])
    other = await _node("второй графовый хит", T)
    hits = [
        {"id": -fact_node - _ID_OFFSET_GRAPH, "source": "graph"},
        {"id": -other - _ID_OFFSET_GRAPH, "source": "graph"},
    ]
    await log_co_pairs(db, "q", hits)
    await log_co_pairs(db, "q", hits)

    first = [await miner_provenance(db, "user"), await miner_co_retrieval(db, "user")]
    assert first[0]["edges"] == 1 and first[1]["edges"] == 1
    conn = await connection_manager.get(DB_NAME)
    count1 = (await (await conn.execute("SELECT COUNT(*) FROM epi_edges")).fetchone())[0]

    second = [await miner_provenance(db, "user"), await miner_co_retrieval(db, "user")]
    assert all(r["edges"] == 0 for r in second)
    count2 = (await (await conn.execute("SELECT COUNT(*) FROM epi_edges")).fetchone())[0]
    assert count2 == count1


# --- (g) Task G4: минер #9 embedding, #3 entities (spaCy), инкрементальный режим ---


async def _seed_vector(text: str, vec: list[float]) -> None:
    """Положить контролируемый вектор в embedding_cache (hash-fallback tag).

    Хэш-fallback даёт случайные векторы — для детерминированного порога
    Jaccard тест сеет свои векторы под тем же cache-tag, который miner прочитает.
    """
    from shared.embeddings import EmbeddingCache, _get_model

    cache = EmbeddingCache()
    await cache.ensure()
    tag = cache._cache_model_tag(_get_model())
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
        (cache._hash_text(text), struct.pack(f"{len(vec)}f", *vec), tag),
    )
    await conn.commit()


_V_ALL_ON = [0.5] * 384  # все 384 бита = 1
_V_NEAR = [(-0.5 if i < 30 else 0.5) for i in range(384)]  # 354 общих бита с _V_ALL_ON, J≈0.92
_V_HALF = [(0.5 if i % 2 == 0 else -0.5) for i in range(384)]  # J≈0.5 со всеми — ниже порога


@pytest.mark.asyncio
async def test_miner_embedding_jaccard_threshold_creates_semantic_overlap(db):
    # разный текст, близкие векторы (одна тема) → semantic_overlap; дальний — нет
    t_near, t_far = "очередь событий починки воркера", "очередь событий воркера починена"
    await _seed_vector(t_near, _V_ALL_ON)
    await _seed_vector(t_far, _V_NEAR)
    await _seed_vector("совершенно посторонний сюжет про ужин", _V_HALF)
    n1 = await _node(t_near, T)
    n2 = await _node(t_far, T)
    await _node("совершенно посторонний сюжет про ужин", T)

    from lifecycle.graph_miners import miner_embedding

    result = await miner_embedding(db, "user")

    assert result["edges"] == 1
    rows = await _edges("semantic_overlap")
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (min(n1, n2), max(n1, n2))
    assert rows[0]["weight"] == pytest.approx(0.5)
    assert "heuristic:embedding" in rows[0]["tags"]


@pytest.mark.asyncio
async def test_miner_embedding_topk_cap_fifteen_per_node(db):
    from collections import Counter

    for _ in range(20):
        await _node("однотипный индикатор синхронизации очередей", T)
    await _seed_vector("однотипный индикатор синхронизации очередей", _V_ALL_ON)

    from lifecycle.graph_miners import miner_embedding

    result = await miner_embedding(db, "user")

    rows = await _edges("semantic_overlap")
    assert result["edges"] == len(rows)
    degree: Counter[int] = Counter()
    for r in rows:
        degree[r["source_id"]] += 1
        degree[r["target_id"]] += 1
    assert max(degree.values()) <= 15  # top-k=15 на узел
    assert len(rows) <= 20 * 15 // 2


@pytest.mark.asyncio
async def test_miner_embedding_skips_tool_junk(db):
    await _node('{"type": "tool_result", "tool_use_id": "t1", "content": "raw"}', T)
    await _node("обычный текст про деплой сервиса", T)

    from lifecycle.graph_miners import miner_embedding

    assert (await miner_embedding(db, "user"))["edges"] == 0
    assert await _edges("semantic_overlap") == []


@pytest.mark.asyncio
async def test_miner_entities_synonym_canon_creates_co_mentions(db):
    # «Лили» и «Lily» — один канон-класс сущности (словарь rag.synonyms, обе стороны)
    n1 = await _node("Лили принесла отчёт по проекту", T)
    n2 = await _node("Lily обновила документацию", T)
    await _node("деплой прошёл успешно", T)  # другое слово из словаря — не общая сущность

    from lifecycle.graph_miners import miner_entities

    result = await miner_entities(db, "user")

    assert result["edges"] == 1
    rows = await _edges("co_mentions")
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (min(n1, n2), max(n1, n2))
    assert rows[0]["weight"] == pytest.approx(0.4)
    assert "heuristic:entities" in rows[0]["tags"]


@pytest.mark.asyncio
async def test_miner_entities_spacy_org_shared_mention(db):
    try:
        from mcp_server.utils.privacy import _get_nlp

        _get_nlp()
    except Exception:
        pytest.skip("en_core_web_sm не установлена")
    n1 = await _node("Борис работает в Acme Corp", T)
    n2 = await _node("офис Acme Corp открыт давно", T)

    from lifecycle.graph_miners import miner_entities

    await miner_entities(db, "user")

    rows = await _edges("co_mentions")
    assert (min(n1, n2), max(n1, n2)) in {(r["source_id"], r["target_id"]) for r in rows}


class _FakeL3:
    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        return 1


class _DistillMem:
    """Ровно то, что distill_and_route читает: _cm (для CoreMemory/wire) + l3."""

    def __init__(self, cm: Any) -> None:
        self._cm = cm
        self.l3 = _FakeL3()


@pytest.mark.asyncio
async def test_incremental_wiring_edges_immediately_on_distill(db):
    """(c) запись нового узла через distill_and_route → рёбра сразу, не в ночи."""
    existing = await _node("Лили настроила postgres backup", T)

    from lifecycle.distiller import distill_and_route

    stats = await distill_and_route(_DistillMem(db), object(), "gu", "Лили обновила postgres индексы", 0.8)

    assert stats["l4_saved"] + stats["l3_saved"] >= 1
    conn = await connection_manager.get(DB_NAME)
    new_nodes = await (
        await conn.execute("SELECT node_id FROM epi_nodes WHERE content=? AND node_id != ?", ("Лили обновила postgres индексы", existing))
    ).fetchall()
    assert len(new_nodes) == 1  # атом попал в граф узлом при записи
    edges = await (
        await conn.execute(
            "SELECT relation, tags FROM epi_edges WHERE source_id=? OR target_id=?", (new_nodes[0]["node_id"], new_nodes[0]["node_id"])
        )
    ).fetchall()
    assert len(edges) >= 1  # ребро появилось сразу, ночной batch не нужен
    assert all("heuristic:" in e["tags"] for e in edges)


# --- (d)+(e) теги источника на каждом ребре + идемпотентность ---


@pytest.mark.asyncio
async def test_miners_idempotent_rerun_does_not_duplicate(db):
    await _node("инвариант postgres и wal режима", T, ["postgres"])
    await _node("память postgres и wal режима", T, ["postgres"])
    await _l0("сообщение сессии", T)
    await _l0("второе сообщение сессии", T + 30)

    from lifecycle.graph_miners import miner_sessions, miner_tags, miner_tokens

    first = [await m(db, "user") for m in (miner_tags, miner_tokens, miner_sessions)]
    conn = await connection_manager.get(DB_NAME)
    count1 = (await (await conn.execute("SELECT COUNT(*) FROM epi_edges")).fetchone())[0]
    assert count1 > 0
    assert first[0]["edges"] > 0 and first[1]["edges"] > 0 and first[2]["edges"] > 0  # каждый минер что-то навёл

    second = [await m(db, "user") for m in (miner_tags, miner_tokens, miner_sessions)]
    count2 = (await (await conn.execute("SELECT COUNT(*) FROM epi_edges")).fetchone())[0]

    assert count2 == count1, "повторный вызов не должен дублировать рёбра"
    assert all(r["edges"] == 0 for r in second)
    rows = await (await conn.execute("SELECT tags FROM epi_edges")).fetchall()
    assert all("heuristic:" in r["tags"] for r in rows)  # (d) на каждом ребре


# --- (i) Task G4b: минер #6 маркеры led_to + #8 структурные инварианты ---


@pytest.mark.asyncio
async def test_miner_markers_led_to_creates_edge_in_window(db):
    # A про X (ts=T), B про X с маркером (ts=T+1ч): дельта в [5 мин, 30 дней] → led_to A→B
    nA = await _node("сборка postgres падает с ошибкой", T)
    nB = await _node("postgres починила конфигурацию, теперь работает", T + 3600)
    await _node("полностью посторонний текст про ужин", T + 7200)

    from lifecycle.graph_miners import miner_markers

    result = await miner_markers(db, "user")

    assert result["edges"] == 1
    rows = await _edges("led_to")
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (nA, nB)
    assert rows[0]["weight"] == pytest.approx(0.3)
    assert "heuristic:marker" in rows[0]["tags"]


@pytest.mark.asyncio
async def test_miner_markers_out_of_window_and_direction_no_edge(db):
    from lifecycle.graph_miners import miner_markers

    # дельта > 30 дней
    await _node("сборка postgres падает с ошибкой", T)
    await _node("postgres починила конфигурацию", T + 40 * 86400)
    # дельта < 5 минут
    await _node("очередь воркера зависла намертво", T + 100_000)
    await _node("очередь заданий решено перезапустить", T + 100_060)
    # маркер только в более раннем узле (направление: исход должен идти позже)
    await _node("индексатор сломалось после релиза", T + 200_000)
    await _node("индексатор пересобран заново", T + 203_600)
    # нет общего токена («сборка» ≠ «сборку» — без стемминга)
    await _node("сборка упала с ошибкой", T + 300_000)
    await _node("релиз починила сборку к вечеру", T + 303_600)

    result = await miner_markers(db, "user")

    assert result["edges"] == 0
    assert await _edges("led_to") == []


@pytest.mark.asyncio
async def test_miner_structural_co_citation_creates_edge(db):
    nA = await _node("узел про индексы postgres", T)
    nB = await _node("узел про wal postgres", T)
    nC = await _node("обзорный узел, упомянул обоих", T)
    nE = await _node("узел, упомянул только первого", T)
    await _edge(nC, nA)
    await _edge(nC, nB)  # C цитирует A и B → пара (A,B) co_cited
    await _edge(nE, nA)  # E цитирует только A — новой пары не даёт

    from lifecycle.graph_miners import miner_structural

    result = await miner_structural(db, "user")

    assert result["edges"] == 1
    rows = await _edges("co_cited")
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (min(nA, nB), max(nA, nB))
    assert rows[0]["weight"] == pytest.approx(0.3)  # один общий цитирующий
    assert "heuristic:co_citation" in rows[0]["tags"]
    assert result["boosted"] == 0  # у всех источников дефолтный confidence 0.5

    again = await miner_structural(db, "user")
    assert again["edges"] == 0  # идемпотентно: эвристические рёбра не цитируются повторно


@pytest.mark.asyncio
async def test_miner_structural_belief_propagation_one_shot(db):
    nA = await _node("высококонфидентный источник", T, conf=0.9)
    nB = await _node("цель буста", T)
    nC = await _node("слабый источник", T, conf=0.5)
    nD = await _node("цель без буста", T)
    await _edge(nA, nB, weight=0.5)
    await _edge(nC, nD, weight=0.8)

    from lifecycle.graph_miners import miner_structural

    result = await miner_structural(db, "user")

    assert result["edges"] == 0
    assert result["boosted"] == 1
    conn = await connection_manager.get(DB_NAME)

    async def _confs() -> dict[int, float]:
        rows = await (await conn.execute("SELECT node_id, confidence FROM epi_nodes")).fetchall()
        return {int(r["node_id"]): float(r["confidence"]) for r in rows}

    confs = await _confs()
    assert confs[nB] == pytest.approx(0.5 + 0.1 * 0.9 * 0.5)  # += 0.1·conf(A)·w
    assert confs[nD] == pytest.approx(0.5)  # источник слабже порога 0.8 — не бустит

    again = await miner_structural(db, "user")
    assert again["boosted"] == 0  # одноразовый буст, не рекурсивный
    assert (await _confs())[nB] == pytest.approx(0.545)


@pytest.mark.asyncio
async def test_miner_structural_community_bridge_inside_community(db):
    # звезда: центр nB, листья nA/nC/nD — louvain (seed=42) держит компоненту целиком
    nA = await _node("моста один", T, tags=["postgres"])
    nB = await _node("центр сообщества", T)
    nC = await _node("моста два", T, tags=["postgres"])
    nD = await _node("моста три", T, tags=["linux"])
    await _edge(nA, nB)
    await _edge(nC, nB)
    await _edge(nD, nB)  # источники все разные → co-citation не срабатывает

    from lifecycle.graph_miners import miner_structural

    result = await miner_structural(db, "user")

    assert result["edges"] == 1
    rows = await _edges("community_bridge")
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (min(nA, nC), max(nA, nC))
    assert rows[0]["weight"] == pytest.approx(0.2)
    assert "heuristic:community_bridge" in rows[0]["tags"]
    # пара (A,D) в том же сообществе без прямого ребра, но без общего тега — моста нет

    again = await miner_structural(db, "user")
    assert again["edges"] == 0  # (A,C) уже соединены — повторного моста нет
