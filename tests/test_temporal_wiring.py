"""Temporal graph: layer migration, event recording via primitives, dream digest."""

import asyncio

import pytest


def test_init_adds_layer_to_legacy_table(tmp_path):
    """A pre-layer temporal_events table gains the layer column on ensure()."""
    from graph.temporal import TemporalGraph
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        conn = await cm.get("memory.db")
        await conn.execute(
            """CREATE TABLE temporal_events (
                   event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id TEXT NOT NULL,
                   event_type TEXT NOT NULL,
                   content TEXT NOT NULL,
                   timestamp REAL NOT NULL,
                   importance REAL DEFAULT 0.5,
                   metadata TEXT
               )"""
        )
        await conn.commit()

        tg = TemporalGraph(cm=cm)
        await tg.ensure()  # migration path
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(temporal_events)")).fetchall()]
        assert "layer" in cols

        eid = await tg.add_event("u1", "thought", "hello", layer="agent")
        assert eid > 0
        events = await tg.get_recent("u1", layer="agent")
        assert len(events) == 1 and events[0].event_type == "thought"
        # layer filter excludes other layers
        assert await tg.get_recent("u1", layer="user") == []

    asyncio.run(t())


@pytest.mark.asyncio
async def test_think_records_temporal_event(tmp_path):
    from graph.epistemic import EpistemicGraph
    from graph.temporal import TemporalGraph
    from hooks import UserHooks
    from lifecycle.emotion import EmotionEngine, EmotionTrigger, load_emotion_config
    from mcp_server.tools.primitives import think
    from shared.cache import MemoryCache
    from shared.importance import ImportanceScorer
    from core import MemoryManager
    from wiki import WikiManager

    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await TemporalGraph(cm=cm).ensure()

    class App:
        pass

    app = App()
    app.mm = MemoryManager(cm=cm)
    app.cache = MemoryCache()
    app.temporal = TemporalGraph(cm=cm)

    layer = app.mm.user_memory("tu")
    await layer.l3._init_db()
    await layer.l4._init_db()
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wa"), cm=cm)
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.agent_graph = EpistemicGraph(layer="agent", cm=cm)
    emo_cfg = load_emotion_config()
    app.emotion_engine = EmotionEngine(config=emo_cfg)
    app.emotion_trigger = EmotionTrigger(app.emotion_engine)
    app.importance = ImportanceScorer()
    app.rate_limiter = None
    app.user_hooks = UserHooks()

    class _RC:
        def __init__(self, a):
            self.lifespan_context = a

    class _Ctx:
        def __init__(self, a):
            self.request_context = _RC(a)

    res = await think(text="I decided to use sqlite WAL for storage", layer="agent", user_id="tu", ctx=_Ctx(app))
    assert res["status"] == "ok"

    events = await app.temporal.get_recent("tu", layer="agent")
    assert any(e.event_type == "thought" for e in events)


@pytest.mark.asyncio
async def test_dream_recent_includes_timeline(tmp_path):
    from graph.temporal import TemporalGraph
    from mcp_server.tools.primitives import dream

    class EmptyMulti:
        async def search(self, *a, **k):
            return []

    class EmptyMultiAgent:
        async def search(self, *a, **k):
            return []

    class App:
        pass

    from core import MemoryManager

    app = App()
    app.mm = MemoryManager(cm=None)
    app.mm._cm = None
    app.user_multi = EmptyMulti()
    app.agent_multi = EmptyMultiAgent()
    tg = TemporalGraph()
    app.temporal = tg
    await tg.ensure()
    await app.temporal.add_event("tu2", "personality_shift", "Be precise", importance=1.0, layer="user")

    class _RC:
        def __init__(self, a):
            self.lifespan_context = a

    class _Ctx:
        def __init__(self, a):
            self.request_context = _RC(a)

    # empty search results, but the timeline must still surface
    res = await dream(query="anything", intent="recent", layer="user", user_id="tu2", ctx=_Ctx(app))
    assert "Timeline" in res["summary"]
    assert "personality_shift" in res["summary"]
