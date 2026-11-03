"""D2: get_neighbors must never leave the start node's owner.

The owner is taken from the start node itself, not from a parameter — a
caller that forgot to pass `user_id` is exactly the defect being fixed.
"""

import asyncio

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


async def test_get_neighbors_never_returns_another_owners_nodes(hermetic_graph):
    """A layer-only filter hands `u1` the text of `u2`'s node."""
    g, _ = hermetic_graph
    src = await g.add_node("u1", "u1 hub", "fact")
    mine = await g.add_node("u1", "u1 neighbor", "fact")
    foreign = await g.add_node("u2", "u2 secret", "fact")
    await g.add_edge(src, mine, "knows")
    await g.add_edge(src, foreign, "knows")

    nbrs = await g.get_neighbors(src)
    ids = {n["id"] for n in nbrs}
    assert ids == {mine}, "only the start node's owner may be returned"
    assert all("u2 secret" not in n["content"] for n in nbrs)


async def test_get_neighbors_does_not_walk_through_another_owner(hermetic_graph):
    """A foreign node must not act as a bridge into the owner's own graph."""
    g, _ = hermetic_graph
    src = await g.add_node("u1", "u1 start", "fact")
    foreign = await g.add_node("u2", "u2 bridge", "fact")
    beyond = await g.add_node("u1", "u1 beyond the bridge", "fact")
    await g.add_edge(src, foreign, "knows")
    await g.add_edge(foreign, beyond, "knows")

    nbrs = await g.get_neighbors(src, depth=2)
    assert nbrs == [], "traversal may not cross another owner to reach a node"


async def test_get_neighbors_start_node_in_another_layer_returns_nothing(hermetic_graph):
    """Fail closed: an unknown owner means nothing is returned, not everything."""
    from graph.epistemic import EpistemicGraph

    g, _ = hermetic_graph
    user_node = await g.add_node("u1", "user node", "fact")
    agent = EpistemicGraph(layer="agent", cm=connection_manager)
    agent_node = await agent.add_node("u1", "agent only", "fact")
    # An edge from the other layer's node would leak a user-layer node if the
    # layer of the start node were not checked too.
    await agent.add_edge(agent_node, user_node, "mentions")

    assert await g.get_neighbors(agent_node) == []


async def test_get_neighbors_missing_start_node_returns_nothing(hermetic_graph):
    g, _ = hermetic_graph
    await g.add_node("u1", "lone node", "fact")

    assert await g.get_neighbors(10**9) == []
