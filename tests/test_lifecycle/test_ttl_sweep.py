"""Task 5 (Phase F): TTL-параметр remember + B5-защиты свипа expired."""

import time
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def app(tmp_path) -> AsyncIterator[object]:
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()

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
    app.mm = MM(cm=connection_manager)
    app.cache = MemoryCache()
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"), cm=connection_manager)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wiki_a"), cm=connection_manager)
    app.user_graph = EpistemicGraph(layer="user", cm=connection_manager)
    app.agent_graph = EpistemicGraph(layer="agent", cm=connection_manager)
    emo_cfg = load_emotion_config()
    app.emotion_engine = EmotionEngine(config=emo_cfg)
    app.emotion_trigger = EmotionTrigger(app.emotion_engine)
    app.rate_limiter = RateLimiter()
    app.user_hooks = UserHooks()
    app.agent_hooks = AgentHooks()
    yield app
    connection_manager._conns.clear()


def _ctx(app: object) -> object:
    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx


async def _insert(user_id: str, key: str, kind: str, expires_at: float | None) -> None:
    conn = await connection_manager.get("memory.db")
    now = time.time()
    await conn.execute(
        "INSERT INTO core_memory (user_id, layer, key, value, importance, created_at, updated_at, memory_kind, expires_at)"
        " VALUES (?, 'user', ?, ?, 0.5, ?, ?, ?, ?)",
        (user_id, key, f"v:{key}", now, now, kind, expires_at),
    )
    await conn.commit()


async def _keys(user_id: str) -> list[str]:
    conn = await connection_manager.get("memory.db")
    rows = await (await conn.execute("SELECT key FROM core_memory WHERE user_id=?", (user_id,))).fetchall()
    return [r["key"] for r in rows]


@pytest.mark.asyncio
async def test_remember_ttl_fills_expires_at(app):
    from mcp_server.tools.memory import memory_remember

    ctx = _ctx(app)
    r = await memory_remember(layer="user", user_id="tt", key="eph", value="временный факт", importance=0.5, ttl_minutes=1, ctx=ctx)
    assert r["status"] == "ok"
    conn = await connection_manager.get("memory.db")
    row = await (await conn.execute("SELECT expires_at FROM core_memory WHERE user_id='tt' AND key='eph'")).fetchone()
    now = time.time()
    assert row["expires_at"] is not None and now <= row["expires_at"] <= now + 65

    # ttl_minutes=0 (default) — expires_at остаётся NULL
    await memory_remember(layer="user", user_id="tt", key="perm", value="постоянный факт", importance=0.5, ctx=ctx)
    row = await (await conn.execute("SELECT expires_at FROM core_memory WHERE user_id='tt' AND key='perm'")).fetchone()
    assert row["expires_at"] is None


@pytest.mark.asyncio
async def test_sweep_deletes_only_expired(app):
    from lifecycle.l0_sweep import sweep_expired

    now = time.time()
    for i in range(5):
        await _insert("sw", f"fresh_{i}", "fact", None)
    for i in range(3):
        await _insert("sw", f"old_{i}", "fact", now - 10)

    result = await sweep_expired(min_remain=2)
    assert result == {"deleted": 3, "skipped_reason": None, "remaining": 5}

    keys = await _keys("sw")
    assert all(f"fresh_{i}" in keys for i in range(5))
    assert all(f"old_{i}" not in keys for i in range(3))

    # cleaner_summary пишется в l0_journal (event='l0_sweep')
    conn = await connection_manager.get("memory.db")
    row = await (await conn.execute("SELECT decisions FROM l0_journal WHERE event='l0_sweep' ORDER BY id DESC LIMIT 1")).fetchone()
    assert row is not None and '"deleted": 3' in row["decisions"]


@pytest.mark.asyncio
async def test_sweep_min_remain_shrinks_batch(app):
    from lifecycle.l0_sweep import sweep_expired

    now = time.time()
    for i in range(20):
        await _insert("mr", f"fresh_{i}", "fact", None)
    for i in range(40):
        await _insert("mr", f"old_{i}", "fact", now - 10)

    result = await sweep_expired()  # min_remain=50: после полного свипа осталось бы 20 < 50
    assert result["deleted"] == 10  # партия урезана: удалять только до min_remain
    assert result["remaining"] == 50

    keys = await _keys("mr")
    assert sum(k.startswith("old_") for k in keys) == 30
    assert sum(k.startswith("fresh_") for k in keys) == 20


@pytest.mark.asyncio
async def test_sweep_skips_mass_expiry(app):
    from lifecycle.l0_sweep import sweep_expired

    now = time.time()
    for i in range(10):
        await _insert("mx", f"fresh_{i}", "fact", None)
    for i in range(90):
        await _insert("mx", f"old_{i}", "fact", now - 10)

    result = await sweep_expired()
    assert result == {"skipped": "mass_expiry"}
    # свип ВООБЩЕ не запускается — ничего не удалено
    assert len(await _keys("mx")) == 100


@pytest.mark.asyncio
async def test_sweep_never_touches_never_archive(app):
    from lifecycle.l0_sweep import sweep_expired

    now = time.time()
    await _insert("na", "rule1", "rule", now - 10)
    await _insert("na", "commit1", "commitment", now - 10)
    for i in range(3):
        await _insert("na", f"old_{i}", "fact", now - 10)

    result = await sweep_expired(min_remain=1)
    assert result["deleted"] == 3
    keys = await _keys("na")
    assert "rule1" in keys and "commit1" in keys
