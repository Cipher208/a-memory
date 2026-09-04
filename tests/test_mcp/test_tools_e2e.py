"""End-to-end tests for MCP tool functions — real data flow, temp database.

Each test creates a temporary database via AsyncConnectionManager(base_dir=tmp_path).
No global connection_manager is used, so no aiosqlite hang on exit.
"""

from unittest.mock import MagicMock

import pytest

from mcp_server.tools_layer import (
    memory_context,
    memory_context_inject,
    memory_episode_get,
    memory_episode_list,
    memory_episode_recall,
    memory_episode_save,
    memory_graph_add,
    memory_graph_edges,
    memory_graph_nodes,
    memory_graph_query,
    memory_recall,
    memory_remember,
    memory_session_end,
    memory_session_list,
    memory_session_start,
    memory_stats,
)
from shared.connection import AsyncConnectionManager
from shared.migrations import MigrationManager


@pytest.fixture
async def app(tmp_path):
    """Create real AppContext with temp database — no global connections."""
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    mm = MigrationManager(cm=cm)
    await mm.migrate()

    from core import MemoryManager as MM
    from features.rate_limiting import RateLimiter
    from graph.epistemic import EpistemicGraph
    from hooks.agent_hooks import AgentHooks
    from hooks.user_hooks import UserHooks
    from lifecycle.emotion import EmotionTrigger, EmotionEngine, load_emotion_config
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

    emo_cfg = load_emotion_config()
    app.emotion_engine = EmotionEngine(config=emo_cfg)
    app.emotion_trigger = EmotionTrigger(app.emotion_engine)
    app.rate_limiter = RateLimiter()
    app.user_hooks = UserHooks()
    app.agent_hooks = AgentHooks()
    return app


def _make_ctx(app):
    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx


# ═══════════════════════════════════════════════════════════════
# memory_remember
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_remember_user_full_flow(app):
    ctx = _make_ctx(app)
    result = await memory_remember(layer="user", user_id="eu", key="e_name", value="Alice", importance=0.9, ctx=ctx)
    assert result["status"] == "ok"
    mem = app.mm.user_memory("eu")
    entry = await mem.l4.get("eu", "e_name")
    assert entry is not None
    assert entry.value == "Alice"


@pytest.mark.asyncio
async def test_remember_agent_full_flow(app):
    ctx = _make_ctx(app)
    result = await memory_remember(layer="agent", user_id="ea", key="e_dec", value="Use async", importance=0.8, ctx=ctx)
    assert result["status"] == "ok"
    assert "graph_node_id" in result


@pytest.mark.asyncio
async def test_remember_triggers_hooks(app):
    ctx = _make_ctx(app)
    fired = set()
    # Tool modules import _fire_hook from mcp_server.tools.base at module level,
    # so patching tools_layer._fire_hook has no effect. The seam every caller
    # routes through is hook_registry.fire.
    from hooks.registry import hook_registry

    orig = hook_registry.fire

    async def track(name, layer, ctx, mem=None, graph=None):
        fired.add(name)
        return await orig(name, layer, ctx, mem=mem, graph=graph)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(hook_registry, "fire", track)
        await memory_remember(layer="user", user_id="eh", key="e_hk", value="test", importance=0.5, ctx=ctx)
    assert "message_received" in fired
    assert "emotion_trigger" in fired


# ═══════════════════════════════════════════════════════════════
# memory_recall
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recall_returns_stored_data(app):
    ctx = _make_ctx(app)
    await memory_remember(layer="user", user_id="er", key="e_lang", value="Python", importance=0.7, ctx=ctx)
    result = await memory_recall(layer="user", user_id="er", query="e_lang", ctx=ctx)
    assert len(result["results"]) > 0


@pytest.mark.asyncio
async def test_recall_empty_returns_empty(app):
    ctx = _make_ctx(app)
    result = await memory_recall(layer="user", user_id="exy湖区", query="nonexistent_xyz_abc", ctx=ctx)
    assert result["results"] == []


# memory_forget removed — strict subset of the `forget` primitive (scope=exact
# also cleans L3 + graph + shadow bin); use the primitive for exact deletion.


# ═══════════════════════════════════════════════════════════════
# memory_session_start / end
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_session_lifecycle(app):
    ctx = _make_ctx(app)
    start = await memory_session_start(layer="user", user_id="es", ctx=ctx)
    assert "session_id" in start
    end = await memory_session_end(layer="user", user_id="es", session_id=start["session_id"], summary="done", ctx=ctx)
    assert end["status"] == "ok"


# ═══════════════════════════════════════════════════════════════
# memory_episode_save / recall
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_episode_save_and_recall(app):
    ctx = _make_ctx(app)
    save = await memory_episode_save(layer="user", user_id="ee", summary="Coffee", weight=0.7, tags=["social"], ctx=ctx)
    assert "episode_id" in save
    recall = await memory_episode_recall(layer="user", user_id="ee", ctx=ctx)
    assert len(recall["episodes"]) > 0


# ═══════════════════════════════════════════════════════════════
# memory_graph_add / query
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_graph_add_and_query(app):
    ctx = _make_ctx(app)
    add = await memory_graph_add(layer="user", user_id="eg", content="Python AI", node_type="fact", tags=["py"], ctx=ctx)
    assert "node_id" in add
    q = await memory_graph_query(layer="user", user_id="eg", node_type="fact", ctx=ctx)
    assert len(q["nodes"]) > 0


# ═══════════════════════════════════════════════════════════════
# memory_stats
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stats_after_operations(app):
    ctx = _make_ctx(app)
    await memory_remember(layer="user", user_id="est", key="e_k", value="v", importance=0.5, ctx=ctx)
    result = await memory_stats(layer="user", user_id="est", ctx=ctx)
    assert isinstance(result, dict)
    assert result["l4_facts"] >= 1


# ═══════════════════════════════════════════════════════════════
# memory_context_inject
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_context_inject(app):
    ctx = _make_ctx(app)
    await memory_remember(layer="user", user_id="eci", key="e_col", value="blue", importance=0.7, ctx=ctx)
    result = await memory_context_inject(layer="user", user_id="eci", ctx=ctx)
    assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════
# memory_context
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_context(app):
    ctx = _make_ctx(app)
    await memory_remember(layer="user", user_id="ec2", key="e_ck", value="cv", importance=0.5, ctx=ctx)
    result = await memory_context(layer="user", user_id="ec2", ctx=ctx)
    assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════
# memory_session_list / episode_list / episode_get
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_session_list(app):
    ctx = _make_ctx(app)
    result = await memory_session_list(layer="user", user_id="esl", ctx=ctx)
    assert isinstance(result, dict)
    assert "sessions" in result


@pytest.mark.asyncio
async def test_episode_list(app):
    ctx = _make_ctx(app)
    result = await memory_episode_list(layer="user", user_id="eel", ctx=ctx)
    assert isinstance(result, dict)
    assert "episodes" in result


@pytest.mark.asyncio
async def test_episode_get(app):
    ctx = _make_ctx(app)
    result = await memory_episode_get(layer="user", user_id="eeg", episode_id=999999, ctx=ctx)
    assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════
# memory_graph_nodes / edges
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_graph_nodes(app):
    ctx = _make_ctx(app)
    result = await memory_graph_nodes(layer="user", user_id="egn", ctx=ctx)
    assert isinstance(result, dict)
    assert "nodes" in result


@pytest.mark.asyncio
async def test_graph_edges(app):
    ctx = _make_ctx(app)
    result = await memory_graph_edges(layer="user", user_id="ege", ctx=ctx)
    assert isinstance(result, dict)
    assert "edges" in result


# ═══════════════════════════════════════════════════════════════
# Hook dispatch — verify ALL hooks fire through tools
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_hook_dispatch_all_tools(app):
    """Verify hooks fire through tool calls."""
    ctx = _make_ctx(app)
    fired = set()
    # Patch the shared seam — see test_remember_triggers_hooks.
    from hooks.registry import hook_registry

    orig = hook_registry.fire

    async def track(name, layer, ctx, mem=None, graph=None):
        fired.add(name)
        return await orig(name, layer, ctx, mem=mem, graph=graph)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(hook_registry, "fire", track)

        await memory_remember(layer="user", user_id="eha", key="e_hk2", value="hook test", importance=0.5, ctx=ctx)

        await memory_episode_save(layer="user", user_id="eha", summary="Ep", weight=0.7, ctx=ctx)

        start = await memory_session_start(layer="user", user_id="eha", ctx=ctx)
        await memory_session_end(layer="user", user_id="eha", session_id=start["session_id"], summary="done", ctx=ctx)

        await memory_graph_add(layer="user", user_id="eha", content="Err", node_type="error_analysis", ctx=ctx)
        await memory_graph_add(layer="user", user_id="eha", content="Dec", node_type="decision_log", ctx=ctx)

        await memory_recall(layer="user", user_id="eha", query="test", ctx=ctx)
        await memory_context_inject(layer="user", user_id="eha", ctx=ctx)

        expected = {
            "message_received",
            "emotion_trigger",
            "state_delta",
            "consolidation",
            "error_occurred",
            "decision_made",
            "retrieval_router",
            "auto_context",
        }
        missing = expected - fired
        assert not missing, f"Hooks not fired: {missing}"
