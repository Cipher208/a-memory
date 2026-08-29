"""B1.2: Social Memory Graph — entity upsert + relates_to edges."""

import asyncio

from graph.epistemic import SOCIAL_NODE_TYPES, SOCIAL_RELATIONS, EpistemicGraph
from shared.connection import AsyncConnectionManager


def _make_graph(tmp_path) -> tuple[AsyncConnectionManager, EpistemicGraph]:
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    graph = EpistemicGraph(cm=cm, layer="user")

    async def init():
        await graph.init_db()

    asyncio.run(init())
    return cm, graph


def test_social_vocabulary():
    assert "person" in SOCIAL_NODE_TYPES
    assert "organization" in SOCIAL_NODE_TYPES
    assert "knows" in SOCIAL_RELATIONS
    assert "works_with" in SOCIAL_RELATIONS


def test_find_or_add_entity_dedup(tmp_path):
    """Second add of the same name returns the existing node (created=False)."""
    _, graph = _make_graph(tmp_path)

    async def t():
        nid1, created1 = await graph.find_or_add_entity("u1", "Алиса")
        nid2, created2 = await graph.find_or_add_entity("u1", "Алиса")
        assert created1 is True
        assert created2 is False
        assert nid1 == nid2
        # Different entity type -> separate node
        org_id, created3 = await graph.find_or_add_entity("u1", "Алиса", entity_type="organization")
        assert created3 is True
        assert org_id != nid1

    asyncio.run(t())


def test_find_or_add_entity_scopes_by_user(tmp_path):
    """Same name under different users = different nodes."""
    _, graph = _make_graph(tmp_path)

    async def t():
        a, _ = await graph.find_or_add_entity("u1", "Борис")
        b, _ = await graph.find_or_add_entity("u2", "Борис")
        assert a != b

    asyncio.run(t())


def test_tool_social_upsert_and_edge(tmp_path):
    """memory_graph_add with node_type=person dedups; relates_to adds an edge."""
    from mcp_server.tools.graph import memory_graph_edges
    from mcp_server.tools.graph import memory_graph_add as tool_add

    from tests.test_mcp.test_tools_unit import _make_ctx

    _, graph = _make_graph(tmp_path)
    ctx, app = _make_ctx()
    app.user_graph = graph

    async def t():
        bob, _ = await graph.find_or_add_entity("u1", "Борис")

        r1 = await tool_add(layer="user", user_id="u1", content="Алиса", node_type="person", ctx=ctx)
        assert r1["created"] is True
        r2 = await tool_add(layer="user", user_id="u1", content="Алиса", node_type="person", ctx=ctx)
        assert r2["created"] is False
        assert r1["node_id"] == r2["node_id"]

        r3 = await tool_add(
            layer="user",
            user_id="u1",
            content="Алиса знает Бориса",
            node_type="fact",
            relates_to=bob,
            relation="knows",
            ctx=ctx,
        )
        assert r3["node_id"] > 0
        assert r3["created"] is None  # upsert flag only for social entity types
        edges = await memory_graph_edges(layer="user", user_id="u1", node_id=r3["node_id"], direction="both", ctx=ctx)
        assert edges["count"] == 1
        assert edges["edges"][0]["relation"] == "knows"

    asyncio.run(t())
