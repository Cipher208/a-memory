"""Tests for the universal primitives: auto-routing, layer registry, exposure gate."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.tools.base import (
    LayerBinding,
    _get_memory,
    _validate_layer,
    get_layer,
    register_layer,
)
from mcp_server.tools.primitives import _auto_route, think


# ── think auto-routing ──


def test_auto_route_agent_voice():
    assert _auto_route("I decided to use sqlite WAL for the store") == "agent"
    assert _auto_route("my approach to consolidation changed today") == "agent"
    assert _auto_route("decision_log: picked registry pattern over plugins") == "agent"


def test_auto_route_user_facts():
    assert _auto_route("the user likes short answers") == "user"
    assert _auto_route("Murat prefers Russian for conversation") == "user"


def test_auto_route_tie_defaults_user():
    # no signals either way → user layer
    assert _auto_route("random note about nothing") == "user"


@pytest.mark.asyncio
async def test_think_explicit_layer_wins():
    ctx, app = _make_ctx()
    mem = MagicMock()
    mem.remember = AsyncMock(return_value=1)
    mem.l3 = MagicMock()
    mem.l3.save = AsyncMock(return_value=1)
    app.mm.agent_memory.return_value = mem
    result = await think(text="I decided this", layer="agent", user_id="t1", ctx=ctx)
    assert result["routing"]["resolved_layer"] == "agent"
    app.mm.agent_memory.assert_called_with("t1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I decided to drop the cache table", "agent"),
        ("user wants a summary every morning", "user"),
    ],
)
async def test_think_auto_routes(text, expected):
    ctx, app = _make_ctx()
    mem = MagicMock()
    mem.remember = AsyncMock(return_value=1)
    mem.l3 = MagicMock()
    mem.l3.save = AsyncMock(return_value=1)
    app.mm.user_memory.return_value = mem
    app.mm.agent_memory.return_value = mem
    result = await think(text=text, layer="auto", user_id="t2", ctx=ctx)
    assert result["routing"]["resolved_layer"] == expected


# ── layer registry ──


def test_validate_layer_known():
    assert _validate_layer("User") == "user"
    with pytest.raises(ValueError, match="Invalid layer"):
        _validate_layer("nope")


def test_register_layer_extends_every_resolver():
    binding = LayerBinding(
        memory=lambda app, uid: "skill-memory",
        graph=lambda app: "skill-graph",
        wiki=lambda app: "skill-wiki",
        hooks=lambda app: "skill-hooks",
        rag=lambda app: "skill-rag",
    )
    register_layer("skill", binding)

    assert get_layer("skill") is binding
    assert _validate_layer("SKILL") == "skill"
    assert _get_memory(MagicMock(), "skill", "u1") == "skill-memory"

    # cleanup so other tests don't see the fake layer
    from mcp_server.tools import base

    del base._LAYER_BINDINGS["skill"]


# ── helpers ──


def _make_ctx():
    ctx = MagicMock()
    app = MagicMock()
    app.mm = MagicMock()
    app.rate_limiter = MagicMock()
    app.rate_limiter.check = AsyncMock(return_value={"allowed": True, "remaining": 100, "reset_in": 60})
    app.importance = MagicMock()
    scorer = MagicMock()
    scorer.score = 0.5
    scorer.signals.emotional = 0.0
    app.importance.score = MagicMock(return_value=scorer)
    mem = MagicMock()
    mem.remember = AsyncMock(return_value=1)
    mem.l3 = MagicMock()
    mem.l3.save = AsyncMock(return_value=1)
    graph = MagicMock()
    graph.add_node = AsyncMock(return_value=1)
    wiki = MagicMock()
    wiki.add = AsyncMock(return_value="w/thought")
    app.mm.user_memory.return_value = mem
    app.mm.agent_memory.return_value = mem
    app.user_graph = graph
    app.user_multi = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx, app
