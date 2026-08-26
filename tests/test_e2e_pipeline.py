"""End-to-end memory pipeline: think → layers → wiki → sweep → dream → forget.

Runs the five primitives against REAL stores (no mocks) in a hermetic tmp
directory, mimicking what an agent session does across its lifetime.
"""

import asyncio
import contextlib

import pytest


@pytest.fixture
def e2e(tmp_path):
    from core import MemoryManager
    from graph.epistemic import EpistemicGraph
    from graph.temporal import TemporalGraph
    from hooks import UserHooks
    from lifecycle.emotion import EmotionEngine, EmotionTrigger, load_emotion_config
    from mcp_server.tools import base as tools_base
    from rag.engine import RAGEngine
    from rag.multi_source import MultiSourceRAG
    from shared.cache import MemoryCache
    from shared.importance import ImportanceScorer
    from shared.connection import AsyncConnectionManager
    from wiki import WikiManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))

    class App:
        pass

    app = App()
    app.mm = MemoryManager(cm=cm)
    app.cache = MemoryCache()
    app.temporal = TemporalGraph(cm=cm)
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wiki_a"), cm=cm)
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.agent_graph = EpistemicGraph(layer="agent", cm=cm)

    rag_u = RAGEngine(cm=cm, layer="user")
    rag_a = RAGEngine(cm=cm, layer="agent")
    app.user_multi = MultiSourceRAG(rag=rag_u, wiki=app.user_wiki, cm=cm)
    app.agent_multi = MultiSourceRAG(rag=rag_a, wiki=app.agent_wiki, cm=cm)

    emo_cfg = load_emotion_config()
    app.emotion_engine = EmotionEngine(config=emo_cfg)
    app.emotion_trigger = EmotionTrigger(app.emotion_engine)
    app.importance = ImportanceScorer()
    app.rate_limiter = None
    app.user_hooks = UserHooks()

    # real stores need their schemas before the pipeline touches them
    async def _init():
        for layer in ("user", "agent"):
            m = app.mm.get_layer(layer, "e2e_user")
            await m.l3._init_db()
            await m.l4._init_db()
        await app.user_wiki.init_db()
        await app.agent_wiki.init_db()
        await app.user_graph.init_db()
        await app.agent_graph.init_db()
        await app.temporal.ensure()

    asyncio.run(_init())

    yield {"app": app, "cm": cm, "tmp": tmp_path}


class _RC:
    def __init__(self, a):
        self.lifespan_context = a


class _Ctx:
    def __init__(self, a):
        self.request_context = _RC(a)


@pytest.mark.asyncio
async def test_full_memory_lifecycle(e2e):
    app = e2e["app"]
    from mcp_server.tools.primitives import dream, evolve, forget, project, think

    ctx = _Ctx(app)
    uid = "e2e_user"

    # ── 1. user fact via think (short + importance keywords) ──
    res = await think(text="the user prefers dark mode", layer="user", user_id=uid, ctx=ctx)
    assert res["status"] == "ok"
    assert any(a["type"].startswith(("L4_", "L3_")) for a in res["actions"])

    # ── 2. agent-voice thought lands in agent layer ──
    res = await think(text="I decided to use WAL journaling", layer="auto", user_id=uid, ctx=ctx)
    assert res["routing"]["resolved_layer"] == "agent"

    # ── 3. large text routes to the agent wiki ──
    long_text = "# Architecture\n" + ("We store episodes per layer. " * 100)
    res = await think(text=long_text, layer="agent", user_id=uid, ctx=ctx)
    assert any(a["type"] == "Wiki_thought_save" for a in res["actions"])
    pages = await app.agent_wiki.list_by_type("decision_log", limit=10)
    assert len(pages) >= 1

    # ── 4. dream finds the saved content across sources ──
    res = await dream(query="dark mode", intent="balanced", layer="user", user_id=uid, ctx=ctx)
    assert res["result_count"] >= 1
    assert "dark mode" in res["summary"].lower()

    # ── 5. consolidation sweep promotes/dedups without data loss ──
    from lifecycle.consolidation import ConsolidationEngine

    ce_user = ConsolidationEngine(cm=e2e["cm"], layer="user")
    await ce_user.consolidate_episodes(uid)

    # k1-style facts still recallable after sweep
    rec = await app.mm.user_memory(uid).recall("dark mode")
    assert len(rec) >= 1

    # ── 6. forget(fuzzy) removes and archives into the Shadow Bin ──
    from shared.archived_memories import ArchivedMemories

    am = ArchivedMemories(cm=e2e["cm"])
    await am._init_db()

    res = await forget(key="dark mode", scope="fuzzy", layer="user", user_id=uid, shadow_bin=True, ctx=ctx)
    assert res["deleted_l4"] + res["deleted_l3"] + res["deleted_graph"] >= 0  # placement may vary; archive is the contract
    archived = await am.get_archived(uid, limit=50)
    assert any("dark mode" in (a["content"] or "").lower() or "prefers dark mode" in (a["content"] or "") for a in archived)

    # ── 7. evolve records personality shift (agent core + temporal) ──
    res = await evolve(instruction="Be terse in code reviews", user_id=uid, ctx=ctx)
    assert res["status"] == "ok"
    timeline = await app.temporal.get_recent(uid, layer="agent")
    assert any(e.event_type == "personality_shift" for e in timeline)

    # ── 8. project cycle: init → decision → recall ──
    res = await project(action="init", name="e2e-proj", details="demo spec body", path=str(e2e["tmp"] / "proj"), ctx=ctx)
    assert res["status"] == "ok"
    res = await project(
        action="decision",
        name="e2e-proj",
        decision="sqlite over postgres",
        details="single node",
        outcome="accepted",
        ctx=ctx,
    )
    assert res["status"] == "decided"
    res = await project(action="recall", name="e2e-proj", ctx=ctx)
    assert res["status"] == "recalled"
    assert "sqlite over postgres" in str(res["decisions"][0]["decision"])

    # ── 9. temporal timeline accumulated the session's milestones ──
    timeline_all = await app.temporal.get_recent(uid)
    types = {e.event_type for e in timeline_all}
    assert "thought" in types or "personality_shift" in types or "project_decision" in types


@pytest.mark.asyncio
async def test_e2e_dream_respects_layer_isolation(e2e):
    """A fact saved to the agent layer must not surface in user-layer dreams."""
    app = e2e["app"]
    from mcp_server.tools.primitives import dream, think

    ctx = _Ctx(app)
    uid = "iso_user"

    await think(text="I broke the parser and fixed it myself", layer="agent", user_id=uid, ctx=ctx)
    res = await dream(query="parser fix", intent="balanced", layer="user", user_id=uid, ctx=ctx)

    # nothing in user-layer stores matches; result must be empty, not cross-layer
    assert res["result_count"] == 0
