"""F105 — ``memory_graph_edges`` scopes the layer on BOTH endpoints.

The user half of this asymmetry was closed by F54 (``AND s.user_id = ? AND
t.user_id = ?``); the layer half was missed: only the source node was filtered,
so an edge leaving the requested layer returned the foreign layer's target
content. Each test names the mutation it kills.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


def _ctx(app: object) -> object:
    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx


@pytest.fixture
async def layer_db(tmp_path, monkeypatch):
    """A migrated temp database wired into the global manager, no live bases."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


def _app(cm: object) -> SimpleNamespace:
    from core import MemoryManager
    from graph.epistemic import EpistemicGraph

    return SimpleNamespace(mm=MemoryManager(cm=cm), user_graph=EpistemicGraph(layer="user", cm=cm))


async def _node(cm: object, layer: str, user_id: str, content: str) -> int:
    conn = await cm.get("memory.db")
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, 'fact', '[]', 0.5, ?)",
        (layer, user_id, content, time.time()),
    )
    await conn.commit()
    return int(cur.lastrowid)


async def _edge(cm: object, src: int, tgt: int, relation: str) -> None:
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, 0.8, ?)",
        (src, tgt, relation, time.time()),
    )
    await conn.commit()


async def _seed_cross_layer(cm: object) -> dict[str, int]:
    """user A/B in layer ``user``, agent C in layer ``agent`` (all user ``default``)."""
    a = await _node(cm, "user", "default", "user-a")
    b = await _node(cm, "user", "default", "user-b")
    c = await _node(cm, "agent", "default", "agent-c")
    await _edge(cm, a, b, "same_layer")
    await _edge(cm, a, c, "user_to_agent")
    await _edge(cm, c, a, "agent_to_user")
    return {"a": a, "b": b, "c": c}


def _relations(out: dict[str, object]) -> set[str]:
    return {str(edge["relation"]) for edge in out["edges"]}  # type: ignore[index]


def _contents(out: dict[str, object]) -> set[str]:
    seen: set[str] = set()
    for edge in out["edges"]:  # type: ignore[index]
        seen.add(str(edge["source_content"]))
        seen.add(str(edge["target_content"]))
    return seen


async def test_same_layer_edge_survives(layer_db) -> None:
    """Kills over-filtering: the legitimate A→B edge must not disappear."""
    await _seed_cross_layer(layer_db)

    from mcp_server.tools.graph import memory_graph_edges

    out = await memory_graph_edges(layer="user", user_id="default", ctx=_ctx(_app(layer_db)))

    assert "same_layer" in _relations(out), f"same-layer edge lost: {out}"


async def test_out_does_not_cross_the_layer(layer_db) -> None:
    """Kills the bug: ``s.layer`` only, so A→C leaks agent content on the target."""
    await _seed_cross_layer(layer_db)

    from mcp_server.tools.graph import memory_graph_edges

    out = await memory_graph_edges(layer="user", user_id="default", direction="out", ctx=_ctx(_app(layer_db)))

    assert "user_to_agent" not in _relations(out), f"cross-layer edge returned on out: {out}"
    assert "agent-c" not in _contents(out), f"agent content leaked on out: {out}"


async def test_in_does_not_cross_the_layer(layer_db) -> None:
    """Kills the bug for ``direction="in"``: the requested node is the target, so
    a user-layer source reaching an agent-layer target must not return it."""
    ids = await _seed_cross_layer(layer_db)

    from mcp_server.tools.graph import memory_graph_edges

    out = await memory_graph_edges(layer="user", user_id="default", node_id=ids["c"], direction="in", ctx=_ctx(_app(layer_db)))

    assert "user_to_agent" not in _relations(out), f"cross-layer backlink returned on in: {out}"
    assert "agent-c" not in _contents(out), f"agent content leaked on in: {out}"


async def test_both_does_not_cross_the_layer(layer_db) -> None:
    """Kills the bug for ``direction="both"``: either endpoint must stay in-layer."""
    ids = await _seed_cross_layer(layer_db)

    from mcp_server.tools.graph import memory_graph_edges

    out = await memory_graph_edges(layer="user", user_id="default", node_id=ids["a"], direction="both", ctx=_ctx(_app(layer_db)))

    assert "agent-c" not in _contents(out), f"agent content leaked on both: {out}"
    assert _relations(out) == {"same_layer"}, f"both must keep only the same-layer edge: {out}"


async def test_explicit_user_id_still_scopes_both_endpoints(layer_db) -> None:
    """Guard (stays green): F54's user predicate is untouched by the layer fix."""
    a = await _node(layer_db, "user", "default", "default-a")
    b = await _node(layer_db, "user", "default", "default-b")
    bob = await _node(layer_db, "user", "bob", "bob-x")
    await _edge(layer_db, a, b, "default_rel")
    await _edge(layer_db, a, bob, "default_to_bob")
    await _edge(layer_db, bob, a, "bob_to_default")

    from mcp_server.tools.graph import memory_graph_edges

    out = await memory_graph_edges(layer="user", user_id="default", direction="both", ctx=_ctx(_app(layer_db)))

    assert _relations(out) == {"default_rel"}, f"foreign user's edges leaked: {out}"
    assert "bob-x" not in _contents(out), f"foreign user's content leaked: {out}"
