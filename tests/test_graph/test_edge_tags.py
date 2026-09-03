"""A2.4: edge tags on epi_edges — metadata for traversal filters (_inverse, _value_regex)."""

import asyncio
import sqlite3
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    from graph.epistemic import EpistemicGraph

    yield EpistemicGraph(layer="user", cm=connection_manager), tmp_path
    connection_manager._conns.clear()


async def test_migration_adds_tags_column(hermetic_graph):
    _, tmp = hermetic_graph
    conn = sqlite3.connect(tmp / "memory.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(epi_edges)").fetchall()}
    conn.close()
    assert "tags" in cols


async def test_add_edge_with_tags(hermetic_graph):
    g, tmp = hermetic_graph
    src = await g.add_node("u1", "action node", "action")
    dst = await g.add_node("u1", "outcome node", "outcome")
    await g.add_edge(src, dst, "led_to", weight=0.9, tags=["_inverse:blocked_by", "strength:0.9"])

    conn = sqlite3.connect(tmp / "memory.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT tags FROM epi_edges WHERE source_id=? AND target_id=?", (src, dst)).fetchone()
    conn.close()
    assert row is not None and "_inverse:blocked_by" in (row["tags"] or "")


async def test_get_neighbors_filters_by_tag(hermetic_graph):
    g, _ = hermetic_graph
    src = await g.add_node("u1", "hub", "fact")
    a = await g.add_node("u1", "neighbor tagged", "fact")
    b = await g.add_node("u1", "neighbor plain", "fact")
    await g.add_edge(src, a, "led_to", tags=["_value_regex:deploy.*"])
    await g.add_edge(src, b, "knows")

    tagged = await g.get_neighbors(src, relation="led_to", tag="_value_regex")
    plain = await g.get_neighbors(src, relation="knows")
    assert any(n.get("id") == a for n in tagged)
    assert any(n.get("id") == b for n in plain)
    assert all(n.get("id") != b for n in tagged)


async def test_add_edge_backcompat_no_tags(hermetic_graph):
    g, _ = hermetic_graph
    src = await g.add_node("u1", "a", "fact")
    dst = await g.add_node("u1", "b", "fact")
    await g.add_edge(src, dst, "knows")  # no tags — old call sites keep working
    assert True
