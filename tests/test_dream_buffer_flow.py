"""DreamBuffer restore: hook stages output, sweep consolidates into L4."""

from unittest.mock import MagicMock

import pytest

from core.projects import ProjectMemory  # noqa: F401 — ensures package import parity
from shared.connection import AsyncConnectionManager
from shared.dream_buffer import DreamBuffer
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    manager = AsyncConnectionManager(base_dir=tmp_path)
    await MigrationManager(cm=manager).migrate()
    return manager


def _make_ctx(app):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx


@pytest.mark.asyncio
async def test_dream_hook_stages_into_buffer(tmp_path):
    """The user-layer dream_buffer handler stages summaries via the caller's cm."""
    from hooks.user_hooks import UserHooks

    cm = AsyncConnectionManager(base_dir=tmp_path)

    hooks = UserHooks(user_id="du")
    mem = MagicMock()
    mem._cm = cm
    res = await hooks._dream_buffer({"summary": "deployed v2 to prod", "user_id": "du"}, mem=mem)
    assert res["action"] == "add_to_staging"

    buf = DreamBuffer(cm=cm, layer="user")
    items = await buf.get_staging("du")
    assert len(items) == 1 and "v2" in items[0]["content"], f"staged={items}"

    # agent layer stays empty — staging is layer-isolated
    agent_buf = DreamBuffer(cm=cm, layer="agent")
    assert await agent_buf.get_staging("du") == []


@pytest.mark.asyncio
async def test_sweep_consolidates_staging_into_l4(cm):
    """consolidate_staging promotes staged content; clear empties the buffer."""
    from lifecycle.consolidation import ConsolidationEngine

    buf = DreamBuffer(cm=cm, layer="user")
    await buf.add("su", session_id="dream", content="critical decision: switched to wal mode always", importance=0.9)

    items = await buf.get_staging("su")
    engine = ConsolidationEngine(cm=cm, layer="user")
    res = await engine.consolidate_staging("su", items, min_importance=0.7)
    assert res["promoted"] == 1

    await buf.clear_staging("su")
    assert await buf.get_staging("su") == []

    # landed in L4 as a fact
    from core.memory import CoreMemory

    l4 = CoreMemory(cm=cm, layer="user")
    hits = await l4.search("su", "wal mode", limit=5)
    assert any("wal mode" in h["value"] for h in hits)
