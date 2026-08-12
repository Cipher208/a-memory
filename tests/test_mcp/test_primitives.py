import pytest
from unittest.mock import AsyncMock, MagicMock
from mcp_server.tools.primitives import think, dream


@pytest.mark.asyncio
async def test_dream_search_and_budgeting():
    """Test dream tool hybrid search and token budgeting."""
    ctx, app = _make_ctx()

    # Mock search results
    mock_results = [{"title": f"Doc {i}", "content": "Some content " * 10, "source": "rag"} for i in range(5)]
    app.user_multi.search = AsyncMock(return_value=mock_results)

    # Test normal summary
    result = await dream(query="test query", ctx=ctx)
    assert "Doc 0" in result["summary"]
    assert result["result_count"] == 5
    assert result["truncated"] is False

    # Test budgeting (mocking _truncate_to_budget via long content)
    long_results = [{"title": "Huge Doc", "content": "A" * 10000, "source": "wiki"}]
    app.user_multi.search = AsyncMock(return_value=long_results)
    result_truncated = await dream(query="test budget", ctx=ctx)
    assert result_truncated["truncated"] is True
    assert "[...truncated to token budget]" in result_truncated["summary"]


@pytest.mark.asyncio
async def test_think_routing_l4():
    """Test think routes short, important text to L4."""
    ctx, app = _make_ctx()
    # Mock importance score: high for short text
    app.importance.score = MagicMock()
    app.importance.score.return_value.score = 0.9
    app.importance.score.return_value.signals.emotional = 0.1

    mem = app.mm.user_memory.return_value
    mem.remember = AsyncMock(return_value=1)
    mem.l3.save = AsyncMock()

    result = await think(text="Short important thought", ctx=ctx)

    assert result["status"] == "ok"
    assert any(a["type"] == "L4_remember" for a in result["actions"])
    mem.remember.assert_called_once()
    mem.l3.save.assert_not_called()


@pytest.mark.asyncio
async def test_think_routing_l3_length():
    """Test think routes long text to L3."""
    ctx, app = _make_ctx()
    app.importance.score = MagicMock()
    app.importance.score.return_value.score = 0.4
    app.importance.score.return_value.signals.emotional = 0.1

    mem = app.mm.user_memory.return_value
    mem.remember = AsyncMock()
    mem.l3.save = AsyncMock(return_value=1)

    long_text = "This is a very long text that should definitely exceed the sixty character limit for episodic memory storage."
    result = await think(text=long_text, ctx=ctx)

    assert result["status"] == "ok"
    assert any(a["type"] == "L3_episodic_save" for a in result["actions"])
    mem.l3.save.assert_called_once()
    mem.remember.assert_not_called()


@pytest.mark.asyncio
async def test_think_routing_l3_emotion():
    """Test think routes emotional text to L3."""
    ctx, app = _make_ctx()
    app.importance.score = MagicMock()
    app.importance.score.return_value.score = 0.5
    app.importance.score.return_value.signals.emotional = 0.8

    mem = app.mm.user_memory.return_value
    mem.remember = AsyncMock()
    mem.l3.save = AsyncMock(return_value=1)

    result = await think(text="I am very happy today!", ctx=ctx)

    assert result["status"] == "ok"
    assert any(a["type"] == "L3_episodic_save" for a in result["actions"])
    mem.l3.save.assert_called_once()


@pytest.mark.asyncio
async def test_think_routing_graph():
    """Test think routes relations to graph."""
    ctx, app = _make_ctx()
    app.importance.score = MagicMock()
    app.importance.score.return_value.score = 0.5
    app.importance.score.return_value.signals.emotional = 0.1

    app.user_graph.add_node = AsyncMock(return_value=1)

    result = await think(text="Alice is related to Bob", ctx=ctx)

    assert result["status"] == "ok"
    assert any(a["type"] == "Graph_node_add" for a in result["actions"])
    app.user_graph.add_node.assert_called_once()


def _make_ctx():
    """Helper to create mock ctx."""
    ctx = MagicMock()
    app = MagicMock()
    app.mm = MagicMock()
    app.rate_limiter = MagicMock()
    app.rate_limiter.check = AsyncMock(return_value={"allowed": True})
    app.importance = MagicMock()
    app.user_graph = MagicMock()
    app.agent_graph = MagicMock()
    app.hook_registry = MagicMock()
    app.hook_registry.fire = AsyncMock(return_value={})
    ctx.request_context.lifespan_context = app
    return ctx, app
