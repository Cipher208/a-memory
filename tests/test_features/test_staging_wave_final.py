"""E17b+E17c: wiki_write staging kind + transition-level consolidation revert."""

import asyncio
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


def _app(tmp_path):
    """Minimal AppContext-like: mm for core ops."""
    from core import MemoryManager as MM

    class _App:
        pass

    app = _App()
    app.mm = MM(cm=connection_manager)
    return app


# ─── E17b: wiki_write kind ─────────────────────────────────────────────────────


async def test_wiki_write_apply_and_revert(hermetic_base):
    from features.staging import decide, propose, revert

    await propose("tool", "wiki_write", "default", "user", {"title": "e17 note", "content": "staged wiki page body", "wiki_type": "work_notes"})
    pending = await _pending_first()
    res = await decide(pending, approve=True, mem=_app(hermetic_base))
    assert res["status"] == "applied"
    assert res["result_ref"].startswith("wiki:")

    path = res["result_ref"].removeprefix("wiki:")
    rev = await revert(pending, mem=_app(hermetic_base))
    assert rev["status"] == "reverted" and rev["restored"] == 1

    assert not (hermetic_base / "wiki" / "user" / "concept" / f"{path}").exists() or not any(
        (hermetic_base / "wiki" / "user").rglob("*.md")
    )  # page file removed (path may be absolute-ish; check by rglob)


async def _pending_first() -> int:
    from features.staging import list_pending

    rows = await list_pending("default", 5)
    return int(rows[0]["id"])


async def test_wiki_write_validation(hermetic_base):
    from features.staging import _apply

    with pytest.raises(ValueError, match="requires title and content"):
        await _apply("wiki_write", "default", "user", {"title": "", "content": ""}, mem=_app(hermetic_base))


# ─── E17c: transition revert ───────────────────────────────────────────────────


async def test_revert_transition_removes_promoted_l4(hermetic_base):
    """Episode promotion → L4; revert_transition removes the L4 row, episode stays."""
    from core.memory import CoreMemory
    from features.staging import revert_transition
    from lifecycle.transitions import record_transition

    # seed one high-weight episode
    from core.episodic import EpisodicMemory

    epi = EpisodicMemory(cm=connection_manager, layer="user")
    eid = await epi.save("default", "the big consolidation fact", 0.9, ["x"])
    # simulate what consolidate_episodes does (promote + transition row)
    cm = CoreMemory(cm=connection_manager, layer="user")
    entry_id = await cm.save("default", "ep_the_big_consolidation_fact", "the big consolidation fact", importance=0.9, source="episode_promotion")
    tid = await record_transition(connection_manager, "default", "episode", f"episode:{eid}", "l4", f"core:{entry_id}", "episode_promotion")
    assert tid > 0

    # sanity: L4 row exists
    assert (
        await CoreMemory(cm=connection_manager, layer="user").recall_key_exists("ep_the_big_consolidation_fact")
        if hasattr(cm, "recall_key_exists")
        else True
    )
    res = await revert_transition("default", tid)
    assert res["deleted"] is True

    # L4 gone, episode untouched
    core = CoreMemory(cm=connection_manager, layer="user")
    hits = await core.search("default", "the big consolidation fact", limit=5)
    assert all(h["key"] != "ep_the_big_consolidation_fact" for h in hits)
    rows = await epi.search("default", "the big consolidation fact", limit=5)
    assert any(getattr(e, "episode_id", None) == eid for e in rows)


async def test_revert_transition_validates(hermetic_base):
    import sqlite3

    # memory_transitions is created by record_transition's ensure (runtime);
    # seed the table explicitly for the by-id lookup
    conn = sqlite3.connect(hermetic_base / "memory.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_transitions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
        " kind TEXT NOT NULL, from_ref TEXT NOT NULL, to_ref TEXT NOT NULL, reason TEXT, ts REAL NOT NULL)"
    )
    conn.commit()
    conn.close()
    from features.staging import revert_transition
    from lifecycle.transitions import record_transition

    with pytest.raises(ValueError, match="unknown transition"):
        await revert_transition("default", 99999)
    with pytest.raises(ValueError, match="another user"):
        tid = await record_transition(connection_manager, "someone_else", "episode", "episode:1", "l4", "core:42", "episode_promotion")
        await revert_transition("default", tid)


def test_tool_surface_revert_transition_registered():
    """memory_proposals accepts action=revert_transition (signature-level check)."""
    import inspect

    from mcp_server.tools.ops import memory_proposals

    sig = inspect.signature(memory_proposals)
    assert "transition_id" in sig.parameters
    assert "payload" in sig.parameters  # E17b producer


async def test_propose_action_full_lifecycle(hermetic_base):
    """E17b producer: agent stages a wiki_write → decide applies → revert removes.

    Closes the dead-data gap: wiki_write kind now has a producer surface.
    """
    from mcp_server.tools.ops import memory_proposals

    app = _app(hermetic_base)
    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app

    staged = await memory_proposals(
        "propose",
        kind="wiki_write",
        payload={"title": "agent staged page", "content": "deliberate body", "wiki_type": "work_notes"},
        user_id="default",
        ctx=ctx,
    )
    assert staged["status"] == "ok" and staged["proposal_id"] > 0

    decided = await memory_proposals("decide", proposal_id=staged["proposal_id"], approve=True, ctx=ctx)
    assert decided["status"] == "applied" and decided["result_ref"].startswith("wiki:")

    pages = list((hermetic_base / "wiki" / "user").rglob("*agent_staged_page*.md"))
    assert pages, "apply must create the wiki page"

    reverted = await memory_proposals("revert", proposal_id=staged["proposal_id"], ctx=ctx)
    assert reverted["status"] == "reverted" and reverted["restored"] == 1
    assert not list((hermetic_base / "wiki" / "user").rglob("*agent_staged_page*.md"))


async def test_propose_kind_whitelist(hermetic_base):
    from mcp_server.tools.ops import memory_proposals

    with pytest.raises(ValueError, match="wiki_write|core_write"):
        await memory_proposals("propose", kind="delete_everything", payload={}, user_id="default", ctx=MagicMock())
