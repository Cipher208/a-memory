"""Phase G Task 2: минеры #1 tags, #2 token-overlap, #4 sessions.

Фикстура: epi_nodes с тегами (epi_tags), текстами, L0-записями одной сессии.
Каждое ребро минера обязано нести tags LIKE '%heuristic:<name>%'; повторный
вызов не дублирует рёбра (INSERT OR IGNORE по PK epi_edges).
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


async def _node(content: str, ts: float, tags: list[str] | None = None) -> int:
    """Факт-узел с контролем created_at + прямая запись в epi_tags."""
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
        " VALUES ('user', 'gu', ?, 'fact', ?, 0.5, ?)",
        (content, json.dumps(tags or []), ts),
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
    return await (
        await conn.execute("SELECT * FROM epi_edges WHERE relation=? ORDER BY source_id, target_id", (relation,))
    ).fetchall()


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
    assert by_pair[(min(n1, n2), max(n1, n2))]["weight"] == pytest.approx(0.4)  # 1 общий тег
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
