"""Tests for mcp_server/tools_layer.py — tools with correct mock ctx."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.tools_layer import (
    _fire_hook,
    memory_episode_save,
    memory_forget,
    memory_graph_add,
    memory_recall,
    memory_remember,
    memory_session_end,
    memory_session_start,
    memory_stats,
)

# ── Helpers ──


@pytest.mark.asyncio
async def test_fire_hook_no_handlers():
    result = await _fire_hook("nonexistent_hook", "user", {})
    assert result.get("skipped") is True


# ── Mock ctx helper ──


def _make_ctx(layer="user"):
    """Create mock MCP ctx with AppContext."""
    ctx = MagicMock()
    app = MagicMock()
    app.mm = MagicMock()
    app.rate_limiter = MagicMock()
    app.rate_limiter.check = AsyncMock(return_value={"allowed": True, "remaining": 100, "reset_in": 60})
    app.emotion_trigger = MagicMock()
    app.emotion_trigger.should_save = MagicMock(return_value=(False, "", 0.0))
    app.user_hooks = MagicMock()
    app.agent_hooks = MagicMock()
    app.user_graph = MagicMock()
    app.user_graph.add_node = AsyncMock(return_value=1)
    app.agent_graph = MagicMock()
    app.agent_graph.add_node = AsyncMock(return_value=1)
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx, app


# ── memory_remember ──


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["user", "agent"])
async def test_remember(layer):
    ctx, app = _make_ctx()
    mem = app.mm.user_memory.return_value if layer == "user" else app.mm.agent_memory.return_value
    mem.remember = AsyncMock(return_value=1)
    if layer == "agent":
        app.agent_graph.add_node = AsyncMock(return_value=1)
    result = await memory_remember(layer=layer, user_id="u1", key="k", value="v", ctx=ctx)
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_remember_rate_limited():
    ctx, app = _make_ctx()
    app.rate_limiter.check = AsyncMock(return_value={"allowed": False, "remaining": 0, "reset_in": 60})
    result = await memory_remember(layer="user", user_id="u1", key="k", value="v", ctx=ctx)
    assert "error" in result


@pytest.mark.asyncio
async def test_remember_invalid_layer():
    ctx, _ = _make_ctx()
    with pytest.raises(ValueError, match="Invalid layer"):
        await memory_remember(layer="bad", user_id="u1", key="k", value="v", ctx=ctx)


# ── memory_recall ──


@pytest.mark.asyncio
async def test_recall():
    ctx, app = _make_ctx()
    app.mm.user_memory.return_value.recall = AsyncMock(return_value=[{"key": "n"}])
    result = await memory_recall(layer="user", user_id="u1", query="name", ctx=ctx)
    assert "results" in result


# ── memory_forget ──


@pytest.mark.asyncio
async def test_forget():
    ctx, app = _make_ctx()
    app.mm.user_memory.return_value.forget = AsyncMock(return_value=True)
    result = await memory_forget(layer="user", user_id="u1", key="k", ctx=ctx)
    assert result.get("deleted_l4", 0) >= 1


# ── memory_session_start/end ──


@pytest.mark.asyncio
async def test_session_start():
    ctx, app = _make_ctx()
    app.mm.user_memory.return_value.l2 = MagicMock()
    app.mm.user_memory.return_value.l2.create_session = AsyncMock(return_value="s1")
    result = await memory_session_start(layer="user", user_id="u1", ctx=ctx)
    assert "session_id" in result


@pytest.mark.asyncio
async def test_session_end():
    ctx, app = _make_ctx()
    app.mm.user_memory.return_value.l2 = MagicMock()
    app.mm.user_memory.return_value.l2.close_session = AsyncMock()
    result = await memory_session_end(layer="user", user_id="u1", session_id="s1", summary="done", ctx=ctx)
    assert result["status"] == "ok"


# ── memory_episode_save ──


@pytest.mark.asyncio
async def test_episode_save():
    ctx, app = _make_ctx()
    app.mm.user_memory.return_value.l3 = MagicMock()
    app.mm.user_memory.return_value.l3.save = AsyncMock(return_value=1)
    result = await memory_episode_save(layer="user", user_id="u1", summary="Event", weight=0.8, ctx=ctx)
    assert "episode_id" in result


# ── memory_graph_add ──


@pytest.mark.asyncio
async def test_graph_add():
    ctx, app = _make_ctx()
    app.user_graph.add_node = AsyncMock(return_value=1)
    result = await memory_graph_add(layer="user", user_id="u1", content="Fact", node_type="fact", ctx=ctx)
    assert "node_id" in result


# ── memory_stats ──


@pytest.mark.asyncio
async def test_stats(monkeypatch):
    ctx, app = _make_ctx()
    # recall_count path reads app.mm._cm -> patch the store to avoid MagicMock int()
    import features.recall_telemetry as rt

    monkeypatch.setattr(rt, "count_recalls", AsyncMock(return_value=0))
    mem = app.mm.user_memory.return_value
    mem.l1 = MagicMock()
    mem.l1.size = MagicMock(return_value=0)
    mem.l2 = MagicMock()
    mem.l2.count_sessions = AsyncMock(return_value=0)
    mem.l2.avg_quality = AsyncMock(return_value=None)
    mem.l3 = MagicMock()
    mem.l3.count = AsyncMock(return_value=0)
    mem.l4 = MagicMock()
    mem.l4.count = AsyncMock(return_value=0)
    wiki = app.user_wiki
    wiki.count = AsyncMock(return_value=0)
    graph = app.user_graph
    graph.count_nodes = AsyncMock(return_value=0)
    result = await memory_stats(layer="user", user_id="u1", ctx=ctx)
    assert isinstance(result, dict)
