"""A1 wiki chain: graph builder (A1.5), wiki_query BFS (A1.4), wiki_reflect (A1.3), MOC (A1.1)."""

import asyncio

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic_wiki(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    from wiki.manager import WikiManager

    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"))
    asyncio.run(wm.init_db())  # production path: lifespan inits wiki schema
    yield wm, tmp_path
    connection_manager._conns.clear()


def _graph(tmp_path):
    from graph.epistemic import EpistemicGraph

    return EpistemicGraph(layer="user", cm=connection_manager)


# ─── A1.5: builder ─────────────────────────────────────────────────────────────


async def test_builder_pages_and_links(hermetic_wiki):
    from lifecycle.wiki_graph_builder import build_from_wiki

    wm, tmp = hermetic_wiki
    await wm.add(wiki_type="work_notes", title="alpha page", content="Alpha body")
    await wm.add(wiki_type="work_notes", title="beta page", content="Beta body follows [[alpha_page]]")

    res = await build_from_wiki(user_id="default", layer="user")
    assert res["pages"] >= 2

    import sqlite3

    conn = sqlite3.connect(tmp / "memory.db")
    wiki_nodes = conn.execute("SELECT COUNT(*) FROM epi_nodes WHERE node_type='wiki_page'").fetchone()[0]
    edge_rels = conn.execute("SELECT relation, COUNT(*) FROM epi_edges GROUP BY relation").fetchall()
    conn.close()
    assert wiki_nodes >= 2
    assert any(rel == "follows" for rel, _ in edge_rels)


async def test_builder_idempotent(hermetic_wiki):
    from lifecycle.wiki_graph_builder import build_from_wiki

    wm, tmp = hermetic_wiki
    await wm.add(wiki_type="work_notes", title="once page", content="Body")
    r1 = await build_from_wiki(user_id="default", layer="user")
    r2 = await build_from_wiki(user_id="default", layer="user")

    import sqlite3

    conn = sqlite3.connect(tmp / "memory.db")
    n = conn.execute("SELECT COUNT(*) FROM epi_nodes WHERE node_type='wiki_page'").fetchone()[0]
    conn.close()
    assert r1["pages"] == r2["pages"] and n == r1["pages"]  # no duplicates


# ─── A1.4: wiki_query BFS ──────────────────────────────────────────────────────


async def test_wiki_query_bfs(hermetic_wiki):
    wm, _tmp = hermetic_wiki
    a = await wm.add(wiki_type="work_notes", title="root page", content="Root")
    b = await wm.add(wiki_type="work_notes", title="mid page", content="Mid")
    c = await wm.add(wiki_type="work_notes", title="leaf page", content="Leaf")
    await wm.index.add_link(a, b, "follows")
    await wm.index.add_link(b, c, "follows")

    from features.wiki_query import wiki_query_bfs

    res = await wiki_query_bfs(a, depth=2, layer="user")
    paths = {r["path"] for r in res["nodes"]}
    assert b in paths and c in paths
    assert any(r["depth"] == 2 for r in res["nodes"])


# ─── A1.3: wiki_reflect ────────────────────────────────────────────────────────


async def test_wiki_reflect(hermetic_wiki):
    wm, _tmp = hermetic_wiki
    for i in range(6):
        await wm.add(wiki_type="work_notes", title=f"page {i}", content=f"content {i}", importance=0.4 + i * 0.1)
    await wm.add(wiki_type="work_notes", title="retire me", content="old stuff")
    # mark one stale via frontmatter

    pages = await wm.list_by_type("work_notes", status=None)
    stale = next(p for p in pages if p.title == "retire me")
    await wm.update(stale.file_path, status="stale")

    from features.wiki_reflect import wiki_reflect

    res = await wiki_reflect(layer="user")
    assert res["totals"]["pages"] >= 7
    assert res["totals"]["stale"] >= 1
    assert res["reflection"]  # non-empty text digest


# ─── A1.1: MOC auto-generation ─────────────────────────────────────────────────


async def test_moc_generated_on_add(hermetic_wiki):
    wm, tmp = hermetic_wiki
    await wm.add(wiki_type="work_notes", title="first topic", content="A [[second_topic]] page")
    await wm.add(wiki_type="work_notes", title="second_topic", content="B page")
    await wm.add(wiki_type="work_notes", title="third topic", content="C page")

    moc_path = tmp / "wiki_u" / "work_notes" / "MOC_work_notes.md"
    assert moc_path.exists(), "MOC hub must auto-generate on category writes (>=3 pages)"
    body = moc_path.read_text()
    assert "first topic" in body and "second_topic" in body


# ─── A1.6: community detection ─────────────────────────────────────────────────


async def test_communities_cluster_linked_pages(hermetic_wiki):
    from lifecycle.wiki_communities import detect_communities
    from lifecycle.wiki_graph_builder import build_from_wiki

    wm, _tmp = hermetic_wiki
    a = await wm.add(wiki_type="work_notes", title="hub page", content="Hub")
    b = await wm.add(wiki_type="work_notes", title="linked page", content="Linked")
    await wm.add(wiki_type="work_notes", title="loner page", content="Loner")
    await wm.index.add_link(a, b, "follows")
    await build_from_wiki(user_id="default", layer="user")

    res = await detect_communities(user_id="default", layer="user")
    assert res["node_count"] >= 3
    assert any(c["size"] >= 2 for c in res["communities"]), "the linked pair must land in one community"
    all_pages = {p for c in res["communities"] for p in c["pages"]}
    assert any("hub_page" in p for p in all_pages) and any("loner_page" in p for p in all_pages)
