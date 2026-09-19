"""Phase G Task 5: graph sanitation — lateral inhibition, validity windows, MAD, valence, hub exclusion.

Фикстура: мигрированная БД (alembic g20 добавляет valid_from/valid_to/status
на epi_edges). Санитария детерминирована: SYNAPSE-формула, MAD по statistics,
фиксированные словари валентности — без LLM.
"""

import json
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


async def _node(content: str, node_type: str = "fact") -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user', 'gu', ?, ?, '[]', 0.5, ?)",
        (content, node_type, T),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _heuristic_edge(a: int, b: int, relation: str, weight: float) -> None:
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)",
        (min(a, b), max(a, b), relation, weight, T, json.dumps([f"heuristic:{relation}"])),
    )
    await conn.commit()


async def _edge_weight(a: int, b: int, relation: str) -> float | None:
    conn = await connection_manager.get(DB_NAME)
    row = await (
        await conn.execute(
            "SELECT weight FROM epi_edges WHERE relation=? AND source_id=? AND target_id=?",
            (relation, min(a, b), max(a, b)),
        )
    ).fetchone()
    return None if row is None else float(row["weight"])


# --- (a) lateral inhibition: û_i = max(0, u_i − β·Σ(u_k−u_i)·𝕀[u_k>u_i]), β=0.15, M=7 ---


@pytest.mark.asyncio
async def test_prune_dangling_edges(db):
    """Central liveness sweep (2026-09-16): edges whose endpoints no longer exist
    are deleted in one pass; valid edges untouched; idempotent."""
    from lifecycle.graph_sanitation import prune_dangling_edges

    a, b = await _node("dangling prune один"), await _node("dangling prune два")
    conn = await connection_manager.get(DB_NAME)
    await _heuristic_edge(a, b, "same_session", 0.3)
    # dangling rows referencing never-existing node ids (stale-writer artifact)
    await conn.execute(
        "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, 'same_session', 0.3, ?, '[]')",
        (a, 999_999, T),
    )
    await conn.execute(
        "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, 'tagged', 0.4, ?, '[]')", (999_998, b, T)
    )
    await conn.commit()

    assert await prune_dangling_edges(connection_manager) == 2
    row = await (await conn.execute("SELECT COUNT(*) c FROM epi_edges WHERE source_id=? AND target_id=?", (min(a, b), max(a, b)))).fetchone()
    assert row["c"] == 1  # the valid edge survived
    assert await prune_dangling_edges(connection_manager) == 0  # idempotent


@pytest.mark.asyncio
async def test_lateral_inhibition_weak_edge_suppressed_by_strong_cluster(db):
    hub = await _node("хаб-узел кластера")
    strong = [await _node(f"сильный сосед {i}") for i in range(7)]
    weak = await _node("слабый одиночка")
    for s in strong:
        await _heuristic_edge(hub, s, "tagged", 0.6)
    await _heuristic_edge(hub, weak, "tagged", 0.3)

    from lifecycle.graph_sanitation import lateral_inhibition

    changed = await lateral_inhibition(await connection_manager.get(DB_NAME), hub)

    assert changed >= 1
    # 0.3 − 0.15·7·(0.6−0.3) = 0.3 − 0.315 < 0 → гасится в ноль
    assert await _edge_weight(hub, weak, "tagged") == pytest.approx(0.0)
    for s in strong:  # равные сильные не давят друг друга (нет строго больших)
        assert await _edge_weight(hub, s, "tagged") == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_lateral_inhibition_top_m_boundary(db):
    # 8 равных сильных: в T_M попадают только 7 (M=7), восьмой не ингибирует
    hub = await _node("хаб с восемью")
    for _ in range(8):
        await _heuristic_edge(hub, await _node("сильный"), "tagged", 0.6)
    late = await _node("чуть слабее")
    await _heuristic_edge(hub, late, "tagged", 0.59)

    from lifecycle.graph_sanitation import lateral_inhibition

    await lateral_inhibition(await connection_manager.get(DB_NAME), hub)

    # 0.59 − 0.15·7·0.01 = 0.5795 (все 8 ингибировали бы до 0.578)
    assert await _edge_weight(hub, late, "tagged") == pytest.approx(0.5795, rel=1e-3)


@pytest.mark.asyncio
async def test_insert_edge_applies_inhibition_on_creation(db):
    """Wiring: _insert_edge гасит слабое ребро сразу после вставки heuristic-ребра."""
    from lifecycle.graph_miners import _insert_edge

    hub = await _node("хаб инкрементального режима")
    for i in range(7):
        await _insert_edge(await connection_manager.get(DB_NAME), hub, await _node(f"крепкий {i}"), "tagged", 0.6, "tags")
    weak = await _node("слабый инкрементальный")
    written = await _insert_edge(await connection_manager.get(DB_NAME), hub, weak, "tagged", 0.3, "tags")

    assert written == 1
    assert await _edge_weight(hub, weak, "tagged") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_insert_edge_repeat_skips_inhibition(db, monkeypatch):
    """Регрессия 19.09, issue G (петля nightly): повторный upsert существующего
    ребра НЕ запускает lateral_inhibition. Иначе каждый nightly заново гонит
    квадратичную ингибицию по всем рёбрам хаба (640k рёбер, 9ч 100% CPU)."""
    import lifecycle.graph_sanitation as san
    from lifecycle.graph_miners import _insert_edge

    calls = 0
    real = san.lateral_inhibition

    async def spy(conn, node_id, *args, **kwargs):
        nonlocal calls
        calls += 1
        return await real(conn, node_id, *args, **kwargs)

    monkeypatch.setattr(san, "lateral_inhibition", spy)
    a = await _node("повтор один")
    b = await _node("повтор два")
    conn = await connection_manager.get(DB_NAME)

    w1 = await _insert_edge(conn, a, b, "topic_overlap", 0.5, "tokens")
    assert w1 == 1
    assert calls == 2, "создание ребра ингибирует оба конца"

    w2 = await _insert_edge(conn, a, b, "topic_overlap", 0.5, "tokens")
    assert w2 == 0, "19.09 ч.3: повтор того же веса — no-op"
    assert calls == 2, "повтор не должен трогать ингибицию"

    w3 = await _insert_edge(conn, a, b, "topic_overlap", 0.2, "tokens")
    assert w3 == 0
    assert calls == 2, "слабый повтор не должен трогать ингибицию"


@pytest.mark.asyncio
async def test_insert_edge_dirty_set_defers_inhibition(db, monkeypatch):
    """19.09, issue G (часть 2): с dirty-набором ингибиция не бежит сразу,
    а копит узлы; _inhibit_dirty — один проход по каждому узлу."""
    import lifecycle.graph_sanitation as san
    from lifecycle.graph_miners import _inhibit_dirty, _insert_edge

    calls: list[int] = []
    real = san.lateral_inhibition

    async def spy(conn, node_id, *args, **kwargs):
        calls.append(int(node_id))
        return await real(conn, node_id, *args, **kwargs)

    monkeypatch.setattr(san, "lateral_inhibition", spy)
    hub = await _node("хаб батча")
    spokes = [await _node(f"спица батча {i}") for i in range(5)]
    conn = await connection_manager.get(DB_NAME)

    dirty: set[int] = set()
    for s in spokes:
        w = await _insert_edge(conn, hub, s, "topic_overlap", 0.5, "tokens", dirty)
        assert w == 1
    assert calls == [], "вставки с dirty не ингибируют сразу"
    assert dirty == {hub, *spokes}

    changed = await _inhibit_dirty(conn, dirty)
    assert isinstance(changed, int)
    assert sorted(calls) == sorted({hub, *spokes}), "ровно один проход по узлу"


@pytest.mark.asyncio
async def test_miner_tags_inhibits_once_per_node(db, monkeypatch):
    """Wiring: miner_tags с хабом из 5 рёбер — 6 вызовов ингибиции, а не 10."""
    import lifecycle.graph_sanitation as san
    from lifecycle.graph_miners import miner_tags

    calls = 0
    real = san.lateral_inhibition

    async def spy(conn, node_id, *args, **kwargs):
        nonlocal calls
        calls += 1
        return await real(conn, node_id, *args, **kwargs)

    monkeypatch.setattr(san, "lateral_inhibition", spy)
    conn = await connection_manager.get(DB_NAME)
    hub = await _node("хаб тегов")
    await conn.execute("INSERT INTO epi_tags (node_id, tag) VALUES (?, ?)", (hub, "общий"))
    for i in range(5):
        s = await _node(f"тег-спица {i}")
        await conn.execute("INSERT INTO epi_tags (node_id, tag) VALUES (?, ?)", (s, "общий"))
    await conn.commit()

    res = await miner_tags(connection_manager, "user")
    assert res["edges"] == 15, "C(6,2) пар на общем теге"
    assert calls == 6, f"батч: 6 узлов — 6 проходов, получено {calls}"


@pytest.mark.asyncio
async def test_insert_edge_weaker_repeat_is_noop(db):
    """19.09, issue G (часть 3): слабый повтор не пишет строку (created_at
    стабилен) и возвращает 0 — иначе каждый nightly churn'ит WAL миллионами
    пустых апдейтов."""
    from lifecycle.graph_miners import _insert_edge

    a = await _node("noop один")
    b = await _node("noop два")
    conn = await connection_manager.get(DB_NAME)

    assert await _insert_edge(conn, a, b, "topic_overlap", 0.6, "tokens") == 1
    before = await (await conn.execute("SELECT created_at FROM epi_edges WHERE source_id=? AND target_id=?", (a, b))).fetchone()
    assert await _insert_edge(conn, a, b, "topic_overlap", 0.2, "tokens") == 0
    after = await (await conn.execute("SELECT weight, created_at FROM epi_edges WHERE source_id=? AND target_id=?", (a, b))).fetchone()
    assert float(after["weight"]) == pytest.approx(0.6)
    assert float(after["created_at"]) == float(before["created_at"])


@pytest.mark.asyncio
async def test_miner_sessions_caps_mega_cluster(db, monkeypatch):
    """19.09, issue G (часть 3): один кластер на 40 узлов не материализует
    полный граф — степень same_session на узел <= _SESSION_TOPK."""
    import lifecycle.graph_sanitation as san
    from lifecycle.graph_miners import _SESSION_TOPK, miner_sessions

    monkeypatch.setattr(san, "lateral_inhibition", lambda *a, **k: _fake_inhibition())
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT INTO l0_journal (ts, event, layer, user_id, text, raw_type) VALUES (?, 'msg', 'user', 'u', 'мега сессия', 'user-message')",
        (T,),
    )
    for i in range(40):
        await conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user', 'gu', ?, 'fact', '[]', 0.5, ?)",
            (f"мега узел {i}", T),
        )
    await conn.commit()

    res = await miner_sessions(connection_manager, "user")
    assert res["edges"] < 40 * 39 // 2, "полный граф запрещён капом"
    rows = await (await conn.execute("SELECT source_id, target_id FROM epi_edges WHERE relation='same_session'")).fetchall()
    deg: dict[int, int] = {}
    for r in rows:
        deg[int(r["source_id"])] = deg.get(int(r["source_id"]), 0) + 1
        deg[int(r["target_id"])] = deg.get(int(r["target_id"]), 0) + 1
    assert max(deg.values()) <= _SESSION_TOPK


async def _fake_inhibition(*args, **kwargs) -> int:
    return 0


# --- (b) validity windows: valid_from/valid_to/status на epi_edges ---


@pytest.mark.asyncio
async def test_validity_window_expired_edge_not_in_active_queries(db):
    a, b = await _node("факт раз"), await _node("факт два")
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags, valid_from, valid_to, status)"
        " VALUES (?, ?, 'tagged', 0.5, ?, '[]', ?, ?, 'active')",
        (a, b, T, T, T - 10),  # valid_to в прошлом
    )
    await conn.commit()

    from lifecycle.graph_sanitation import active_edges_clause, validate_edges

    assert await validate_edges(conn) == 1  # O(|E|) recheck пометил просроченное
    row = await (await conn.execute("SELECT status FROM epi_edges WHERE source_id=? AND target_id=?", (a, b))).fetchone()
    assert row["status"] == "expired"

    clause, params = active_edges_clause()
    rows = await (await conn.execute(f"SELECT relation FROM epi_edges WHERE {clause}", params)).fetchall()
    assert rows == []  # ребро с valid_to < now не возвращается в active-запросах

    # бессрочное ребро (NULL-окна, status=active по умолчанию) остаётся в active
    await _heuristic_edge(a, b, "same_session", 0.3)
    rows = await (await conn.execute(f"SELECT relation FROM epi_edges WHERE {clause}", params)).fetchall()
    assert len(rows) == 1 and rows[0]["relation"] == "same_session"


@pytest.mark.asyncio
async def test_graph_enrich_runs_validity_recheck(db):
    """Wiring: graph_enrich вызывает Sanitation (recheck validity) и отчитывается."""
    a, b = await _node("факт для речека"), await _node("второй для речека")
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags, valid_to) VALUES (?, ?, 'tagged', 0.5, ?, '[]', ?)",
        (a, b, T, T - 10),
    )
    await conn.commit()

    from lifecycle.graph_enrich import graph_enrich

    result = await graph_enrich(layer="user")

    assert result["sanitation"]["expired"] >= 1
    row = await (await conn.execute("SELECT status FROM epi_edges WHERE source_id=?", (a,))).fetchone()
    assert row["status"] == "expired"


# --- (c) MAD-пороги: τ = median − κ·MAD, κ=1.5 ---


def test_mad_threshold_outlier_does_not_move_threshold():
    from lifecycle.graph_sanitation import mad_threshold

    flat = [0.3, 0.3, 0.3, 0.3, 0.5]
    assert mad_threshold(flat) == pytest.approx(0.3)  # med=0.3, MAD=0 → τ=0.3
    assert mad_threshold([*flat, 9.9]) == pytest.approx(0.3)  # outlier не двигает ни median, ни MAD
    scores = [0.40, 0.42, 0.44, 0.46, 0.48]
    # med=0.44, MAD=median([0.04,0.02,0,0.02,0.04])=0.02 → τ=0.44−1.5·0.02
    assert mad_threshold(scores) == pytest.approx(0.41)
    assert mad_threshold([]) == 0.0


# --- (d) valence-typed: словарь relation→valence → buckets ---


def test_valence_classification_supersedes_goes_to_superseded():
    from lifecycle.graph_sanitation import VALENCE_BUCKETS, classify_fact

    assert set(VALENCE_BUCKETS) == {"primary", "supporting", "contrasting", "qualifying", "superseded"}
    assert classify_fact(["supersedes"]) == "superseded"
    assert classify_fact(["supports"]) == "supporting"
    assert classify_fact(["contradicts", "supports"]) == "contrasting"  # противоречие сильнее поддержки
    assert classify_fact(["derives_from"]) == "qualifying"
    assert classify_fact(["tagged", "same_session"]) == "primary"  # эвристические отношения — без валентности
    assert classify_fact([]) == "primary"


# --- (e) hub exclusion: node_type NOT IN ('moc','auto_index') ---


@pytest.mark.asyncio
async def test_hub_exclusion_moc_not_in_centrality_candidates(db):
    fact = await _node("обычный факт")
    moc = await _node("MOC-оглавление", node_type="moc")
    auto = await _node("авто-индекс", node_type="auto_index")
    for i in range(5):  # хаб накручивает degree — без исключения он топ-1 centrality
        await _heuristic_edge(moc, await _node(f"спутник {i}"), "tagged", 0.5)
    await _heuristic_edge(auto, fact, "tagged", 0.5)

    from lifecycle.graph_sanitation import centrality_candidates

    cands = await centrality_candidates(await connection_manager.get(DB_NAME), "user")

    assert fact in cands
    assert moc not in cands and auto not in cands


@pytest.mark.asyncio
async def test_node_communities_excludes_hubs(db):
    """Wiring: louvain (_node_communities) не даёт MOC-хабу склеивать сообщество."""
    a, b = await _node("узел а"), await _node("узел б")
    moc = await _node("MOC-хаб сообществ", node_type="moc")
    await _heuristic_edge(moc, a, "tagged", 0.9)
    await _heuristic_edge(moc, b, "tagged", 0.9)  # через хаб a,b — одно сообщество

    from lifecycle.graph_miners import _node_communities

    comms = await _node_communities(await connection_manager.get(DB_NAME), "user")

    assert all(moc not in c for c in comms)
    assert comms == []  # без хаба a и b изолированы — сообществ нет
