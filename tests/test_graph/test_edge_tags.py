"""A2.4: edge tags on epi_edges — metadata for traversal filters (_inverse, _value_regex)."""

import asyncio
import sqlite3

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


# ── count_edges: the graph's edge numbers, split live/dead ──


async def test_count_edges_splits_live_and_dead(hermetic_graph):
    """A zeroed weight is counted as dead, not dropped and not mixed into live.

    Zero is what `lateral_inhibition` writes and what `_insert_edge` later
    overwrites, so it must be visible as its own number: reporting only a total
    would hide that most of a graph can be sitting at zero.
    """
    g, tmp = hermetic_graph
    a = await g.add_node("u1", "узел a", "fact")
    b = await g.add_node("u1", "узел b", "fact")
    c = await g.add_node("u1", "узел c", "fact")
    await g.add_edge(a, b, "knows", weight=0.8)
    await g.add_edge(b, c, "knows", weight=0.8)

    conn = sqlite3.connect(tmp / "memory.db")
    conn.execute("UPDATE epi_edges SET weight=0 WHERE source_id=? AND target_id=?", (b, c))
    conn.commit()
    conn.close()

    assert await g.count_edges("u1") == {"total": 2, "live": 1, "dead": 1}


async def test_count_edges_is_zeroed_graph(hermetic_graph):
    """No edges at all reports zeros rather than raising or returning None."""
    g, _ = hermetic_graph
    await g.add_node("u1", "одинокий узел", "fact")
    assert await g.count_edges("u1") == {"total": 0, "live": 0, "dead": 0}


async def test_count_edges_uses_source_node_layer(hermetic_graph):
    """epi_edges has no layer column, so an edge belongs to its SOURCE node's layer."""
    g, tmp = hermetic_graph
    user_src = await g.add_node("u1", "user source", "fact")
    user_dst = await g.add_node("u1", "user target", "fact")
    await g.add_edge(user_src, user_dst, "knows")

    agent_src = await g.add_node("u1", "agent source", "fact")
    agent_dst = await g.add_node("u1", "agent target", "fact")
    await g.add_edge(agent_src, agent_dst, "knows")
    conn = sqlite3.connect(tmp / "memory.db")
    conn.execute("UPDATE epi_nodes SET layer='agent' WHERE node_id IN (?,?)", (agent_src, agent_dst))
    conn.commit()
    conn.close()

    from graph.epistemic import EpistemicGraph

    assert await g.count_edges("u1") == {"total": 1, "live": 1, "dead": 0}
    assert await EpistemicGraph(layer="agent", cm=connection_manager).count_edges("u1") == {"total": 1, "live": 1, "dead": 0}


async def test_count_edges_ignores_edges_whose_source_node_is_gone(hermetic_graph):
    """An orphan edge is in no bucket rather than guessed into one."""
    g, tmp = hermetic_graph
    a = await g.add_node("u1", "a", "fact")
    b = await g.add_node("u1", "b", "fact")
    orphan = await g.add_node("u1", "orphan", "fact")
    await g.add_edge(a, b, "knows")
    await g.add_edge(orphan, b, "knows")

    conn = sqlite3.connect(tmp / "memory.db")
    conn.execute("DELETE FROM epi_nodes WHERE node_id=?", (orphan,))
    conn.commit()
    rows = conn.execute("SELECT COUNT(*) FROM epi_edges").fetchone()[0]
    conn.close()

    assert rows == 2, "ребро осталось в таблице"
    assert await g.count_edges("u1") == {"total": 1, "live": 1, "dead": 0}
