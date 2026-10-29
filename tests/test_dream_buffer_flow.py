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


@pytest.mark.asyncio
async def test_clear_staging_leaves_other_layer_intact(cm):
    """Kills: clear_staging deleting both layers for the user.

    `lifespan.py` consolidates per layer, then calls `clear_staging(uid)`; with no
    `layer` in the WHERE the first layer to run wiped the other layer's
    un-consolidated staging. Irreversible.
    """
    user_buf = DreamBuffer(cm=cm, layer="user")
    agent_buf = DreamBuffer(cm=cm, layer="agent")
    await user_buf.ensure()
    await user_buf.add("u", session_id="dream", content="user staged")
    await agent_buf.add("u", session_id="dream", content="agent staged")

    await user_buf.clear_staging("u")

    assert await user_buf.get_staging("u") == [], "the user layer must be drained"
    assert [i["content"] for i in await agent_buf.get_staging("u")] == ["agent staged"], "agent staging was wiped by the user-layer clear"


@pytest.mark.asyncio
async def test_cleanup_old_is_layer_scoped(cm):
    """Kills: cleanup_old deleting by age, or capping, across layers."""
    await DreamBuffer(cm=cm, layer="user").ensure()
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO staging_memories (layer, user_id, session_id, content, created_at)"
        " VALUES ('agent', 'u', 's', 'agent old', datetime('now', '-48 hours'))"
    )
    await conn.execute(
        "INSERT INTO staging_memories (layer, user_id, session_id, content, created_at)"
        " VALUES ('user', 'u', 's', 'user old', datetime('now', '-48 hours'))"
    )
    await conn.commit()

    res = await DreamBuffer(cm=cm, layer="user").cleanup_old(max_age_hours=24)
    assert res["by_age"] == 1, f"the user-layer age delete must take its own row, got {res}"
    left = [r["content"] for r in await (await conn.execute("SELECT content FROM staging_memories")).fetchall()]
    assert left == ["agent old"], f"cleanup_old reached into the agent layer, left={left}"

    # Cap branch: the user layer must be over its own cap, otherwise the GROUP BY
    # loop never runs and `by_count == 0` proves nothing. Agent rows must survive.
    for i in range(3):
        await conn.execute(
            "INSERT INTO staging_memories (layer, user_id, session_id, content) VALUES ('agent', 'u', 's', ?)",
            (f"agent fresh {i}",),
        )
    for i in range(3):
        await conn.execute(
            "INSERT INTO staging_memories (layer, user_id, session_id, content) VALUES ('user', 'u', 's', ?)",
            (f"user fresh {i}",),
        )
    await conn.commit()
    res = await DreamBuffer(cm=cm, layer="user").cleanup_old(max_age_hours=10**6, max_count=1)
    assert res["by_count"] == 2, f"the user-layer cap must trim its own layer, got {res}"
    rows = await (await conn.execute("SELECT layer, content FROM staging_memories")).fetchall()
    agent_left = [r["content"] for r in rows if r["layer"] == "agent"]
    assert len(agent_left) == 4, f"the user-layer cap trimmed the agent layer, agent_left={agent_left}"
    assert len([r for r in rows if r["layer"] == "user"]) == 1


@pytest.mark.asyncio
async def test_clear_staging_session_is_layer_scoped(cm):
    """Kills: the session branch of clear_staging losing its `layer` predicate."""
    user_buf = DreamBuffer(cm=cm, layer="user")
    agent_buf = DreamBuffer(cm=cm, layer="agent")
    await user_buf.ensure()
    await user_buf.add("u", session_id="s1", content="user s1")
    await user_buf.add("u", session_id="s2", content="user s2")
    await agent_buf.add("u", session_id="s1", content="agent s1")

    removed = await user_buf.clear_staging("u", session_id="s1")
    assert removed == 1
    assert [i["content"] for i in await agent_buf.get_staging("u")] == ["agent s1"], "the session clear crossed layers"
    assert [i["content"] for i in await user_buf.get_staging("u")] == ["user s2"], "the session clear took another session"


@pytest.mark.asyncio
async def test_emergency_purge_clears_both_staging_layers(tmp_path):
    """Kills: a purge that only clears the default layer.

    `clear_staging` became layer-scoped in this package, so a purge built on a
    default `DreamBuffer()` would silently leave the agent layer behind — the
    opposite of what an emergency purge promises.
    """
    from shared import connection

    previous_base = connection.connection_manager.base_dir
    connection.connection_manager.base_dir = tmp_path
    connection.connection_manager._conns.clear()
    await MigrationManager(cm=connection.connection_manager).migrate()
    try:
        for layer in ("user", "agent"):
            await DreamBuffer(cm=connection.connection_manager, layer=layer).add("pu", session_id="s", content=f"{layer} staged")

        from mcp_server.tools.ops import _purge_staging

        removed = await _purge_staging("pu")
        assert removed == 2, f"the purge must clear every layer, removed={removed}"
        for layer in ("user", "agent"):
            left = await DreamBuffer(cm=connection.connection_manager, layer=layer).get_staging("pu")
            assert left == [], f"{layer} staging survived the purge: {left}"
    finally:
        connection.connection_manager._conns.clear()
        connection.connection_manager.base_dir = previous_base
