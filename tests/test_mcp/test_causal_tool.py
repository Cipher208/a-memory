"""E17a: causal-link producer surface on memory_graph_add (B1.7 gets its producer)."""

import asyncio
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


def _ctx():
    from types import SimpleNamespace

    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = SimpleNamespace(
        mm=None, cache=None, user_wiki=None, agent_wiki=None, user_graph=None, agent_graph=None
    )
    return ctx


async def test_causal_action_creates_nodes_and_edge(hermetic_base):
    """Full e2e through the real graph layer — epi_edges row lands."""
    from graph.epistemic import EpistemicGraph
    from mcp_server.tools.graph import memory_graph_add

    ctx = _ctx()
    ctx.request_context.lifespan_context.user_graph = EpistemicGraph(layer="user", cm=connection_manager)

    res = await memory_graph_add(
        action="causal",
        content="deployed tag v2",
        outcome="release published",
        relation="led_to",
        ctx=ctx,
        user_id="default",
    )
    assert res["status"] == "ok"
    assert res["action_node"] > 0 and res["outcome_node"] > 0

    import sqlite3

    conn = sqlite3.connect(hermetic_base / "memory.db")
    n_edges = conn.execute("SELECT COUNT(*) FROM epi_edges").fetchone()[0]
    types = [r[0] for r in conn.execute("SELECT node_type FROM epi_nodes").fetchall()]
    conn.close()
    assert n_edges == 1
    assert set(types) == {"action", "outcome"}


async def test_causal_action_validates(hermetic_base):
    from graph.epistemic import EpistemicGraph
    from mcp_server.tools.graph import memory_graph_add

    ctx = _ctx()
    ctx.request_context.lifespan_context.user_graph = EpistemicGraph(layer="user", cm=connection_manager)

    with pytest.raises(ValueError, match="requires content"):
        await memory_graph_add(action="causal", content="", outcome="x", ctx=ctx, user_id="default")
    with pytest.raises(ValueError, match="relation must be one of"):
        await memory_graph_add(action="causal", content="a", outcome="b", relation="knows", ctx=ctx, user_id="default")


async def test_default_action_unchanged(hermetic_base):
    """action='node' (default) keeps the existing single-node behavior."""
    from graph.epistemic import EpistemicGraph
    from mcp_server.tools.graph import memory_graph_add

    ctx = _ctx()
    ctx.request_context.lifespan_context.user_graph = EpistemicGraph(layer="user", cm=connection_manager)
    res = await memory_graph_add(content="solo node", ctx=ctx, user_id="default")
    assert res["node_id"] > 0  # GraphNodeResult shape unchanged
