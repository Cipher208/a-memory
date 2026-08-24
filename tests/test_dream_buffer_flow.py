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
    """dream() end-to-end: hook stages summary into staging (user layer only)."""
    from unittest.mock import AsyncMock, MagicMock

    from mcp_server.tools.primitives import dream
    from shared.migrations import MigrationManager as MMig

    cm = AsyncConnectionManager(base_dir=tmp_path)
    await MMig(cm=cm).migrate()

    from core import MemoryManager as MM
    from features.rate_limiting import RateLimiter
    from graph.epistemic import EpistemicGraph
    from hooks.agent_hooks import AgentHooks
    from hooks.user_hooks import UserHooks
    from lifecycle.emotion import EmotionEngine, EmotionTrigger, load_emotion_config
    from wiki import WikiManager

    class App:
        pass

    app = App()
    app.mm = MM(cm=cm)
    app.cache = None
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wa"), cm=cm)
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.user_multi = AsyncMock()
    app.user_multi.search = AsyncMock(return_value=[{"title": "deploy", "content": "v2 to prod", "source": "wiki"}])
    app.importance = MagicMock()
    emo_cfg = load_emotion_config()
    app.emotion_engine = EmotionEngine(config=emo_cfg)
    app.emotion_trigger = EmotionTrigger(app.emotion_engine)
    app.rate_limiter = RateLimiter()
    app.user_hooks = UserHooks(user_id="du")
    app.agent_hooks = AgentHooks(user_id="du")

    ctx = _make_ctx(app)
    from hooks.registry import hook_registry

    fired = {}

    async def spy(hook_name, layer, context, mem=None):
        out = await hook_registry.fire(hook_name, layer, context, mem=mem)
        fired[hook_name] = {"out": out, "mem_cm": getattr(mem, "_cm", None)}
        return out

    import mcp_server.tools.primitives as prim

    orig = prim._fire_hook
    prim._fire_hook = spy
    try:
        res = await dream(query="deploy state", layer="user", user_id="du", ctx=ctx)
    finally:
        prim._fire_hook = orig
    assert res["summary"], "dream must produce a summary"

    assert "dream_buffer" in fired, f"hook not fired: {list(fired)}"
    info = fired["dream_buffer"]
    assert info["mem_cm"] is cm, "handler must receive the app cm"

    buf = DreamBuffer(cm=cm, layer="user")
    items = await buf.get_staging("du")
    assert len(items) == 1 and "v2" in items[0]["content"], f"staged={items}"
    assert "v2" in items[0]["content"]

    agent_buf = DreamBuffer(cm=cm, layer="agent")
    assert await agent_buf.get_staging("du") == []


def _make_ctx(app):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx


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
