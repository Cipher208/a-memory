"""T16 (аудит 05.09): рёбра max-weight upsert, hooks dedup, replay-гонка, wiki-мосты."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.mark.asyncio
async def test_insert_edge_max_weight_upsert(cm):
    """Повторный минер усиливает связь: weight = max(старый, новый), а не заморозка."""
    from lifecycle.graph_miners import _insert_edge

    conn = await cm.get("memory.db")
    cur1 = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','u1','a','fact','[]',0.5,1)"
    )
    n1 = int(cur1.lastrowid)
    cur2 = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','u1','b','fact','[]',0.5,1)"
    )
    n2 = int(cur2.lastrowid)
    await conn.commit()

    w1 = await _insert_edge(conn, n1, n2, "tagged", 0.3, "tags")
    assert w1 == 1
    w2 = await _insert_edge(conn, n1, n2, "tagged", 0.6, "tags")
    assert w2 == 1, "усиление записывается (upsert, не IGNORE)"
    row = await (await conn.execute("SELECT weight FROM epi_edges WHERE source_id=? AND target_id=?", (n1, n2))).fetchone()
    assert row[0] == pytest.approx(0.6)
    w3 = await _insert_edge(conn, n1, n2, "tagged", 0.2, "tags")
    assert w3 == 1
    row = await (await conn.execute("SELECT weight FROM epi_edges WHERE source_id=? AND target_id=?", (n1, n2))).fetchone()
    assert row[0] == pytest.approx(0.6), "слабый повторный скор не понижает вес"


@pytest.mark.asyncio
async def test_new_message_dedup_by_source_msg_id(cm):
    """Второе событие с тем же source_msg_id не дистиллируется повторно."""
    from hooks.user_hooks import UserHooks
    from shared.l0 import capture

    rid = await capture("new_message", "user", "dd", "решила дедуп проверять по source_msg_id")
    assert rid is not None

    conn = await cm.get("memory.db")
    cur = conn.execute(
        "INSERT INTO memory_dispatch_log (event, source_msg_id, layer, user_id, score, created_at) VALUES ('new_message', ?, 'user', 'dd', 0.5, ?)",
        (rid, time.time()),
    )
    await cur
    in_tx = (await (await conn.execute("SELECT COUNT(*) FROM memory_dispatch_log")).fetchone())[0]
    await conn.commit()
    after_tx = (await (await conn.execute("SELECT COUNT(*) FROM memory_dispatch_log")).fetchone())[0]
    fresh = await connection_manager.get("memory.db")
    after_fresh = (await (await fresh.execute("SELECT COUNT(*) FROM memory_dispatch_log")).fetchone())[0]
    assert in_tx == 1 and after_tx == 1 and after_fresh == 1, f"INSERT виден: in_tx={in_tx} after_tx={after_tx} fresh={after_fresh}"

    hooks = UserHooks()
    # диагностический pre-assert: дедуп-строка видна из того же стора
    pre = await (await (await cm.get("memory.db")).execute("SELECT source_msg_id, user_id FROM memory_dispatch_log")).fetchall()
    assert [(rid, "dd")] == [(r[0], r[1]) for r in pre], f"dispatch-строка видна: {pre}"

    r = await hooks._new_message(
        {"text": "решила дедуп проверять по source_msg_id", "user_id": "dd", "source_msg_id": rid}, mem=MagicMock(), graph=MagicMock()
    )
    assert "duplicate" in r.get("skipped", ""), f"дубль отсечён: {r}"


@pytest.mark.asyncio
async def test_replay_concurrent_claim(cm):
    """Заявленная ('processing') строка вторым replay пропускается."""
    from features.replay import replay
    from shared.l0 import capture

    rid = await capture("new_message", "user", "rc", "решила гонку реплея разбирать клеймом")
    assert rid is not None
    conn = await cm.get("memory.db")
    # имитация: другой процесс уже держит строку в 'processing' (свежая)
    await conn.execute("UPDATE l0_journal SET status='processing', processed_at=? WHERE id=?", (time.time(), rid))
    await conn.commit()

    res = await replay(since_days=1)
    assert res["processed"] == 0, f"чужая claim не перехватывается: {res}"
    row = await (await conn.execute("SELECT status FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row[0] == "processing"


@pytest.mark.asyncio
async def test_replay_stale_processing_reprocessed(cm):
    """Зависший 'processing' (processed_at > 10м назад) пере-разбирается."""
    from features.replay import replay
    from shared.l0 import capture

    rid = await capture("new_message", "user", "rs", "решила зависший реплей пере-разбирать и завершить")
    assert rid is not None
    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status='processing', processed_at=? WHERE id=?", (time.time() - 900.0, rid))
    await conn.commit()

    res = await replay(since_days=1)
    assert res["processed"] == 1, f"зависшая строка добирается: {res}"


@pytest.mark.asyncio
async def test_wiki_fact_links_miner(cm):
    """[[fact:key]] в wiki_index → ребро wiki_page-узел ↔ fact-узел (S5-мост)."""
    import time as _time

    from core.memory import CoreMemory
    from lifecycle.graph_miners import miner_wiki_fact_links

    conn = await cm.get("memory.db")
    # fact-запись + факт-узел с ТОЧНЫМ content
    cmem = CoreMemory(cm=cm, layer="user")
    await cmem._init_db()
    await cmem.save("u1", "fact:backup_enc", "бэкапы шифруются ключом x", importance=0.8, memory_kind="fact")
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','u1','бэкапы шифруются ключом x','fact','[]',0.5,?)",
        (_time.time(),),
    )
    fact_node = int(cur.lastrowid)
    # wiki-страница с [[fact:]]-ссылкой + её wiki_page-узел
    cur = await conn.execute(
        "INSERT INTO wiki_index (layer, wiki_type, title, file_path, content, created_at, updated_at) VALUES ('user','diary','ops','ops','см. [[fact:backup_enc]]',?,?)",
        (_time.time(), _time.time()),
    )
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','u1','ops','wiki_page','[]',0.5,?)",
        (_time.time(),),
    )
    await conn.commit()

    res = await miner_wiki_fact_links(cm, "user")
    assert res["edges"] >= 1, f"wiki↔L4 мост построен: {res}"
    edge = await (await conn.execute("SELECT source_id, target_id FROM epi_edges WHERE relation='wiki_fact_link'")).fetchone()
    assert edge is not None and fact_node in (edge[0], edge[1]), f"ребро между wiki_page и fact: {edge}"


async def _seed_wiki_fact_link(cm, user_id: str = "u2") -> tuple[int, str]:
    """Setup для backlink-теста: fact-запись + fact-узел + wiki-страница с [[fact:]]-ссылкой.

    Возвращает (page_node_id, file_path) — backlink пишет ИМЕННО page node_id
    (узловое пространство, то же, что рёбра и related_facts read)."""
    import time as _time

    from core.memory import CoreMemory

    conn = await cm.get("memory.db")
    cmem = CoreMemory(cm=cm, layer="user")
    await cmem._init_db()
    await cmem.save(user_id, "fact:backup_enc", "бэкапы шифруются ключом x", importance=0.8, memory_kind="fact")
    await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?,'u2','бэкапы шифруются ключом x','fact','[]',0.5,?)",
        (cmem.layer, _time.time()),
    )
    await conn.execute(
        "INSERT INTO wiki_index (layer, wiki_type, title, file_path, content, created_at, updated_at) VALUES (?, 'diary', 'ops', 'ops2', 'см. [[fact:backup_enc]]', ?, ?)",
        (cmem.layer, _time.time(), _time.time()),
    )
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, 'u2', 'ops2', 'wiki_page', '[]', 0.5, ?)",
        (cmem.layer, _time.time()),
    )
    await conn.commit()
    return int(cur.lastrowid or 0), "ops2"


@pytest.mark.asyncio
async def test_wiki_fact_links_backfills_wiki_ids(cm):
    """S19: [[fact:key]]-линк → metadata.wiki_ids содержит entry_id страницы;
    повторный минер не плодит дубли в wiki_ids."""
    from lifecycle.graph_miners import miner_wiki_fact_links

    entry_id, _ = await _seed_wiki_fact_link(cm)
    conn = await cm.get("memory.db")

    first = await miner_wiki_fact_links(cm, "user")
    assert first["edges"] >= 1
    meta = json.loads(
        (await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='u2' AND key='fact:backup_enc'")).fetchone())[0] or "{}"
    )
    assert meta.get("wiki_ids") == [entry_id], f"backlink записан: {meta}"

    # повторный прогон: ребро upsert (rows=0), wiki_ids НЕ дублируются
    await miner_wiki_fact_links(cm, "user")
    meta2 = json.loads(
        (await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='u2' AND key='fact:backup_enc'")).fetchone())[0] or "{}"
    )
    assert meta2.get("wiki_ids") == [entry_id], f"дублей нет: {meta2}"


@pytest.mark.asyncio
async def test_wiki_ids_merges_with_existing_metadata(cm):
    """Мердж сохраняет существующие metadata-ключи (scope, source_raw_id...)."""
    from core.memory import CoreMemory
    from lifecycle.graph_miners import miner_wiki_fact_links

    entry_id, _ = await _seed_wiki_fact_link(cm)
    conn = await cm.get("memory.db")
    cmem = CoreMemory(cm=cm, layer="user")
    row = await (
        await conn.execute("SELECT value, importance, memory_kind, source, metadata FROM core_memory WHERE user_id='u2' AND key='fact:backup_enc'")
    ).fetchone()
    base_meta = json.loads(row[4] or "{}")
    base_meta["scope"] = "later"
    await cmem.save("u2", "fact:backup_enc", str(row[0]), importance=float(row[1]), memory_kind=row[2], source=row[3] or "tool", metadata=base_meta)

    await miner_wiki_fact_links(cm, "user")
    meta = json.loads(
        (await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='u2' AND key='fact:backup_enc'")).fetchone())[0] or "{}"
    )
    assert meta.get("scope") == "later", f"существующие ключи выжили: {meta}"
    assert meta.get("wiki_ids") == [entry_id]
