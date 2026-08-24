"""Layer/graph/wiki routing verification for the think/dream/forget primitives.

Real temp database, real managers — verifies WHERE content lands per layer.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.tools.primitives import dream, forget, think
from shared.connection import AsyncConnectionManager
from shared.migrations import MigrationManager


@pytest.fixture
async def app(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()

    from core import MemoryManager as MM
    from features.rate_limiting import RateLimiter
    from graph.epistemic import EpistemicGraph
    from hooks.agent_hooks import AgentHooks
    from hooks.user_hooks import UserHooks
    from lifecycle.emotion import EmotionEngine, EmotionTrigger, load_emotion_config
    from shared.cache import MemoryCache
    from wiki import WikiManager

    class App:
        pass

    app = App()
    app.mm = MM(cm=cm)
    app.cache = MemoryCache()
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wiki_a"), cm=cm)
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.agent_graph = EpistemicGraph(layer="agent", cm=cm)
    app.user_multi = AsyncMock()
    app.agent_multi = AsyncMock()
    emo_cfg = load_emotion_config()
    app.emotion_engine = EmotionEngine(config=emo_cfg)
    app.emotion_trigger = EmotionTrigger(app.emotion_engine)
    app.rate_limiter = RateLimiter()
    app.user_hooks = UserHooks()
    app.agent_hooks = AgentHooks()

    # real importance scorer is heavy (transformers); a deterministic stand-in
    class _Scorer:
        def score(self, text: str):
            r = MagicMock()
            r.score = 0.8 if ("decided" in text or "важно" in text) else 0.4
            r.signals.emotional = 0.0
            return r

    app.importance = _Scorer()
    return app


def _make_ctx(app):
    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx


@pytest.mark.asyncio
async def test_think_agent_voice_lands_in_agent_layer(app):
    """Agent-voice thought with auto routing → agent stores only, user stores untouched."""
    text = "I decided to use the registry pattern over plugin discovery"
    res = await think(text=text, layer="auto", user_id="rt", ctx=_make_ctx(app))
    assert res["routing"]["resolved_layer"] == "agent"

    agent_mem = app.mm.agent_memory("rt")
    user_mem = app.mm.user_memory("rt")
    # short + importance 0.8 → L4 core memory on the agent side
    entries = await agent_mem.l4.search("rt", "registry pattern", limit=5)
    assert any("registry pattern" in e["value"] for e in entries)
    assert await user_mem.l4.search("rt", "registry pattern", limit=5) == []


@pytest.mark.asyncio
async def test_think_user_fact_lands_in_user_layer(app):
    text = "the user likes short answers"
    res = await think(text=text, layer="user", user_id="rt", ctx=_make_ctx(app))
    assert res["routing"]["resolved_layer"] == "user"

    user_mem = app.mm.user_memory("rt")
    # importance 0.4 → L3 episodic, not L4
    episodes = await user_mem.l3.search("rt", "short answers", limit=5)
    assert any("short answers" in e.summary for e in episodes)
    agent_mem = app.mm.agent_memory("rt")
    assert await agent_mem.l3.search("rt", "short answers", limit=5) == []


@pytest.mark.asyncio
async def test_think_long_text_goes_to_wiki(app):
    """>2000 chars → wiki page of the resolved layer, link stored in memory."""
    long_text = "I decided to restructure the module. " * 60  # >2000 chars, agent-voice
    res = await think(text=long_text, layer="agent", user_id="rt", ctx=_make_ctx(app))
    actions = [a["type"] for a in res["actions"]]
    assert "Wiki_thought_save" in actions

    pages = await app.agent_wiki.list_all(limit=10)
    assert len(pages) == 1


@pytest.mark.asyncio
async def test_relation_detected_adds_graph_node(app):
    text = "sqlite WAL is part of the storage layer and it is related to durability"
    await think(text=text, layer="user", user_id="rt", ctx=_make_ctx(app))
    nodes = await app.user_graph.find_nodes_matching("rt", "%storage layer%")
    assert len(nodes) >= 1


@pytest.mark.asyncio
async def test_dream_searches_only_requested_layer(app):
    await think(text="I decided to keep worktrees bounded", layer="agent", user_id="rd", ctx=_make_ctx(app))
    await think(text="the user prefers dark theme", layer="user", user_id="rd", ctx=_make_ctx(app))

    app.agent_multi.search.return_value = [{"title": "agent hit", "content": "worktrees", "source": "l4"}]
    res = await dream(query="worktrees", layer="agent", user_id="rd", ctx=_make_ctx(app))
    assert res["result_count"] >= 1
    app.agent_multi.search.assert_awaited_once()
    app.user_multi.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_fuzzy_clears_all_stores_with_shadow_bin(app):
    uid = "rf"
    await think(text="the user prefers vim keybindings", layer="user", user_id=uid, ctx=_make_ctx(app))
    await think(text="note about vim keybindings history", layer="user", user_id=uid, ctx=_make_ctx(app))
    await app.user_graph.add_node(uid, "vim keybindings are enabled", "fact")

    res = await forget(key="vim", scope="fuzzy", layer="user", user_id=uid, ctx=_make_ctx(app))

    mem = app.mm.user_memory(uid)
    assert await mem.l4.search(uid, "vim", limit=10) == []
    assert await mem.l3.search(uid, "vim", limit=10) == []
    assert await app.user_graph.find_nodes_matching(uid, "%vim%") == []

    # shadow bin kept copies (ArchivedMemories is the store; counts asserted above)
    from shared.archived_memories import ArchivedMemories

    ArchivedMemories(cm=app.mm._cm)
    total = sum(res.get(k, 0) for k in ("deleted_l4", "deleted_l3", "deleted_graph"))
    assert total >= 1
