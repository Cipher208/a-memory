"""Tests for memory_context_inject's inline consolidation step (dream cycle inject)."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.tools.ops import memory_context_inject


@pytest.fixture
def mock_app(monkeypatch):
    """Patch _get_ctx to return a stub AppContext with empty stores.

    Also stubs out the context_cache to avoid cross-test pollution: the
    module-level `_context_cache` dict in mcp_server.tools.base is shared
    across all tests in the session, which causes later tests to hit cache.
    """
    fake_mem = MagicMock()
    fake_mem.l4.get_all = AsyncMock(return_value=[])
    fake_mem.l3.get_episodes = AsyncMock(return_value=[])
    fake_mem.l1.get_recent = MagicMock(return_value=[])

    fake_wiki = MagicMock()
    fake_wiki.list_all = AsyncMock(return_value=[])

    fake_mm = MagicMock()
    fake_mm.user_memory = MagicMock(return_value=fake_mem)
    fake_mm.agent_memory = MagicMock(return_value=fake_mem)

    fake_app = MagicMock()
    fake_app.mm = fake_mm
    fake_app.user_wiki = fake_wiki
    fake_app.agent_wiki = fake_wiki

    # ops.py does `from mcp_server.registry import _get_ctx` — patch the bound name.
    monkeypatch.setattr("mcp_server.tools.ops._get_ctx", lambda c: fake_app)
    # Bypass the shared 30s context cache: always miss + always set
    monkeypatch.setattr("mcp_server.tools.ops._get_cached", lambda key: None)
    monkeypatch.setattr("mcp_server.tools.ops._set_cached", lambda key, val: None)
    monkeypatch.setattr("mcp_server.tools.ops._invalidate_cache", lambda layer, uid: None)
    return fake_app


@pytest.mark.asyncio
async def test_context_inject_returns_consolidation_fields(mock_app):
    """Result dict must include consolidated_episodes and last_consolidation_ts."""
    fake_engine = MagicMock()
    fake_engine.consolidate_episodes = AsyncMock(return_value=3)

    with patch("lifecycle.consolidation.ConsolidationEngine", return_value=fake_engine):
        result = await memory_context_inject(layer="user", user_id="default")

    assert "consolidated_episodes" in result
    assert "last_consolidation_ts" in result
    assert result["consolidated_episodes"] == 3
    assert isinstance(result["last_consolidation_ts"], float)


@pytest.mark.asyncio
async def test_context_inject_zero_when_nothing_promoted(mock_app):
    fake_engine = MagicMock()
    fake_engine.consolidate_episodes = AsyncMock(return_value=0)

    with patch("lifecycle.consolidation.ConsolidationEngine", return_value=fake_engine):
        result = await memory_context_inject(layer="agent", user_id="alice")

    assert result["consolidated_episodes"] == 0
    assert "last_consolidation_ts" in result


@pytest.mark.asyncio
async def test_context_inject_uses_layer_in_engine_construction(mock_app):
    """Engine must be constructed with the same layer as the call."""
    fake_engine = MagicMock()
    fake_engine.consolidate_episodes = AsyncMock(return_value=0)

    with patch("lifecycle.consolidation.ConsolidationEngine", return_value=fake_engine) as ctor:
        await memory_context_inject(layer="agent", user_id="alice")

    ctor.assert_called_once_with(layer="agent")


@pytest.mark.asyncio
async def test_context_inject_does_not_break_on_consolidation_failure(mock_app, caplog):
    """If ConsolidationEngine raises, inject still returns a valid result."""
    fake_engine = MagicMock()
    fake_engine.consolidate_episodes = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch("lifecycle.consolidation.ConsolidationEngine", return_value=fake_engine),
        caplog.at_level("WARNING", logger="mcp_server.tools.ops"),
    ):
        result = await memory_context_inject(layer="user", user_id="default")

    # Fallback: consolidated=0, but result is still a valid dict
    assert "context" in result
    assert result["consolidated_episodes"] == 0


@pytest.mark.asyncio
async def test_context_inject_caches_with_consolidation_fields(mock_app):
    """Both calls in the same window must carry the new fields."""
    fake_engine = MagicMock()
    fake_engine.consolidate_episodes = AsyncMock(return_value=5)

    with patch("lifecycle.consolidation.ConsolidationEngine", return_value=fake_engine):
        r1 = await memory_context_inject(layer="user", user_id="default")
        r2 = await memory_context_inject(layer="user", user_id="default")

    assert r1["consolidated_episodes"] == 5
    assert r2["consolidated_episodes"] == 5
    assert "last_consolidation_ts" in r1
    assert "last_consolidation_ts" in r2


@pytest.mark.asyncio
async def test_context_inject_returns_timestamp_close_to_now(mock_app):
    """last_consolidation_ts should be ~now (within a few seconds)."""
    fake_engine = MagicMock()
    fake_engine.consolidate_episodes = AsyncMock(return_value=0)

    before = time.time()
    with patch("lifecycle.consolidation.ConsolidationEngine", return_value=fake_engine):
        result = await memory_context_inject(layer="user", user_id="default")
    after = time.time()

    assert before <= result["last_consolidation_ts"] <= after
