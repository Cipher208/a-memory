"""Stage 2 Plan A: ariel URI scheme — parse/resolve/integration."""

from __future__ import annotations

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager
from shared.uris import (
    fact_uri,
    node_uri,
    parse_uri,
    resolve_uri,
    wiki_uri,
)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


# ─── parse: полная матрица форм ────────────────────────────────────────────────


def test_parse_all_stores() -> None:
    p = parse_uri("ariel://user/fact/fact:postgres_stack")
    assert p == {"layer": "user", "store": "fact", "key": "fact:postgres_stack", "rest": "fact:postgres_stack"}
    p = parse_uri("ariel://agent/wiki/work_notes/refs-20260906-x.md")
    assert p["store"] == "wiki" and p["key"] == "work_notes/refs-20260906-x.md"
    p = parse_uri("ariel://user/graph/node/42")
    assert p["store"] == "graph" and p["node_id"] == 42
    p = parse_uri("ariel://user/episode/7")
    assert p["store"] == "episode" and p["id"] == 7
    p = parse_uri("ariel://user/l0/9001")
    assert p["store"] == "l0" and p["id"] == 9001


def test_parse_rejects_garbage_and_reserved() -> None:
    for bad in ("", "https://x", "ariel://", "ariel://user", "ariel://user/nonsense", "ariel://user/graph/node/abc", "ariel://user/episode/x"):
        assert parse_uri(bad) is None, f"мусор отвергнут: {bad!r}"
    p = parse_uri("ariel://user/peer/other-agent/fact:x")
    assert p["reserved"] is True and p["store"] == "peer", "peer-namespace резервируется"


def test_builders_roundtrip() -> None:
    assert fact_uri("user", "fact:x") == "ariel://user/fact/fact:x"
    assert wiki_uri("user", "work_notes/refs-x.md") == "ariel://user/wiki/work_notes/refs-x.md"
    assert node_uri("agent", 12) == "ariel://agent/graph/node/12"
    assert parse_uri(wiki_uri("user", "work_notes/refs-x.md"))["key"] == "work_notes/refs-x.md"


# ─── resolve: реальный стор, изоляция user_id ─────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_fact_isolation_and_miss(db) -> None:
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=db, layer="user")
    await cmem.save("ru", "fact:stack", "склад на PostgreSQL", importance=0.8, memory_kind="fact")
    uri = fact_uri("user", "fact:stack")

    hit = await resolve_uri(db, "user", "ru", uri)
    assert hit and hit["value"] == "склад на PostgreSQL" and hit["uri"] == uri
    # изоляция: другой user тот же URI → miss
    assert await resolve_uri(db, "user", "другой", uri) is None
    # не-ariel строка → None
    assert await resolve_uri(db, "user", "ru", "https://nope") is None


@pytest.mark.asyncio
async def test_resolve_graph_episode_l0(db) -> None:
    import time

    from graph.epistemic import EpistemicGraph

    conn = await db.get("memory.db")
    g = EpistemicGraph(cm=db, layer="user")
    nid = await g.add_node("ru", "Борис работает в Google", "fact")
    hit = await resolve_uri(db, "user", "ru", node_uri("user", nid))
    assert hit and hit["content"] == "Борис работает в Google"
    # чужой layer → miss
    assert await resolve_uri(db, "agent", "ru", node_uri("user", nid)) is None

    cur = await conn.execute(
        "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at) VALUES ('user','ru','событие про деплой',0.5,'[]',?)",
        (time.time(),),
    )
    ep_id = int(cur.lastrowid)
    hit = await resolve_uri(db, "user", "ru", f"ariel://user/episode/{ep_id}")
    assert hit and "деплой" in hit["summary"]

    cur = await conn.execute(
        "INSERT INTO l0_journal (id, ts, event, layer, user_id, text) VALUES (777, ?, 'new_message', 'user', 'ru', 'сырьё для drill-down')",
        (time.time(),),
    )
    await conn.commit()
    hit = await resolve_uri(db, "user", "ru", "ariel://user/l0/777")
    assert hit and hit["text"] == "сырьё для drill-down"


@pytest.mark.asyncio
async def test_resolve_wiki_needs_page(db) -> None:
    from wiki.manager import WikiManager

    wm = WikiManager(layer="user", base_dir=str(db.base_dir / "wiki_u"), cm=db)
    await wm.init_db()
    await wm.add("work_notes", "refs-20260907-test", "# лог\nданные", wiki_type=None) if False else await wm.add(
        "work_notes", "refs-20260907-test", "# лог\nданные"
    )
    hit = await resolve_uri(db, "user", "ru", wiki_uri("user", "work_notes/refs-20260907-test.md"), wiki=wm)
    assert hit and "данные" in hit["content"], f"hit={hit!r}; get direct={await wm.get('work_notes/refs-20260907-test.md') is not None}"
    assert await resolve_uri(db, "user", "ru", wiki_uri("user", "work_notes/missing.md"), wiki=wm) is None


def test_resolve_peer_raises_sync() -> None:
    """peer-namespace зарезервирован — resolve обязан громко отказаться."""
    import asyncio

    from shared.uris import resolve_uri

    try:
        asyncio.run(resolve_uri(connection_manager, "user", "u", "ariel://user/peer/other/fact:x"))
        raised = False
    except ValueError as e:
        raised = "reserved" in str(e)
    assert raised, "peer → ValueError с пометкой reserved"


# ─── интеграция: search/wiki_read/drill_down несут URI ────────────────────────


@pytest.mark.asyncio
async def test_search_items_carry_uri(db) -> None:
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=db, layer="user")
    await cmem.save("iu", "fact:addr", "адрес офиса: Москва", importance=0.9, memory_kind="fact")
    hits = await cmem.search("iu", "адрес", limit=5)
    assert hits and hits[0]["uri"] == "ariel://user/fact/fact:addr"


@pytest.mark.asyncio
async def test_drill_down_accepts_uri(db) -> None:
    import time

    from features.diagnostics import drill_down
    from lifecycle.distiller import distill_and_route

    conn = await db.get("memory.db")
    rid = 5001
    await conn.execute(
        "INSERT INTO l0_journal (id, ts, event, layer, user_id, text) VALUES (?, ?, 'new_message', 'user', 'du', 'я решила переехать на ClickHouse')",
        (rid, time.time()),
    )
    await conn.commit()

    class _L3:
        async def save(self, *a, **k):
            return 1

    class _FakeMem:
        def __init__(self, cm):
            self._cm = cm
            self.l3 = _L3()

    from unittest.mock import MagicMock

    await distill_and_route(_FakeMem(db), MagicMock(), "du", "я решила переехать на ClickHouse", 0.8, source_rid=rid)
    row = await (await conn.execute("SELECT entry_id, key FROM core_memory WHERE user_id='du'")).fetchone()
    uri = fact_uri("user", str(row["key"]))

    d_uri = await drill_down(uri, "du")
    d_int = await drill_down(int(row["entry_id"]), "du")
    assert d_uri["source_raw_id"] == rid and d_int["source_raw_id"] == rid, "URI и entry_id эквивалентны"
    assert d_uri["raw_text"] == d_int["raw_text"]

    # не-fact URI → резолв без провенанса
    from graph.epistemic import EpistemicGraph

    g = EpistemicGraph(cm=db, layer="user")
    nid = await g.add_node("du", "узел без провенанса", "fact")
    d_node = await drill_down(node_uri("user", nid), "du")
    assert d_node["source_raw_id"] is None and d_node["resolved"]["node_id"] == nid
