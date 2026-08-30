"""D2.4: skills evolve from sessions — read telemetry, reinforcement, promotion merge."""

import sqlite3
import time
from types import SimpleNamespace

import pytest

from shared.connection import connection_manager


def _ctx(wiki):
    from types import SimpleNamespace

    app = SimpleNamespace(user_wiki=wiki, agent_wiki=wiki)
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.fixture
async def wiki(tmp_path, monkeypatch):
    from wiki.manager import WikiManager

    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    await connection_manager.close_all()  # drop cached conns from other tests' tmp dirs
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki"))
    await wm.init_db()
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, action TEXT NOT NULL,"
            " layer TEXT NOT NULL, target_id TEXT, details TEXT, timestamp REAL NOT NULL)"
        )
        conn.commit()
    yield wm
    await connection_manager.close_all()  # re-resolve for later tests
    connection_manager.base_dir = original


@pytest.mark.asyncio
async def test_wiki_read_logs_skill_read(wiki):
    from mcp_server.tools.wiki import wiki_add, wiki_read

    ctx = _ctx(wiki)
    await wiki_add(layer="user", title="Deploy Flow", content="ssh then sync", wiki_type="skill", ctx=ctx)
    path = (await wiki.list_by_type("skill"))[0].file_path
    await wiki_read(layer="user", path=path, ctx=ctx)
    conn = sqlite3.connect(str(connection_manager.base_dir / "memory.db"))
    rows = conn.execute("SELECT action, target_id FROM audit_log WHERE action='skill_read'").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0][1] == path


@pytest.mark.asyncio
async def test_skill_reinforce_boosts_read_skills(wiki):
    from features.skill_pipeline import skill_reinforce
    from mcp_server.tools.wiki import wiki_add, wiki_read

    ctx = _ctx(wiki)
    await wiki_add(layer="user", title="Hot Skill", content="x", wiki_type="skill", ctx=ctx)
    path = (await wiki.list_by_type("skill"))[0].file_path
    entry = (await wiki.list_by_type("skill"))[0]
    old_imp = entry.importance
    await wiki_read(layer="user", path=path, ctx=ctx)
    res = await skill_reinforce(wiki, window_hours=24)
    assert res["reinforced"] >= 1, f"reinforce res={res}"
    entry2 = (await wiki.list_by_type("skill"))[0]
    assert entry2.importance > old_imp
    # second run within the window: no NEW reads → no further boost
    res2 = await skill_reinforce(wiki, window_hours=24)
    assert res2["reinforced"] == 0


@pytest.mark.asyncio
async def test_promotion_merges_into_existing_skill(wiki):
    from features.skill_pipeline import promote_episodes
    from mcp_server.tools.wiki import wiki_add

    await wiki_add(layer="user", title="Deploy Flow", content="step one", wiki_type="skill", ctx=_ctx(wiki))
    ep = SimpleNamespace(
        episode_id=11,
        summary="Deploy Flow: also check WAL before restart",
        tags=["dream_skill"],
        created_at=time.time(),
    )

    class _FakeMem:
        class l3:  # noqa: N801
            @staticmethod
            async def get_by_id(eid):
                return ep if eid == 11 else None

            @staticmethod
            async def add_tag(eid, tag):
                return True

    res = await promote_episodes(_FakeMem(), wiki, "u1", [11])
    assert res["count"] == 1, f"promote res={res}"
    pages = await wiki.list_by_type("skill")
    assert len(pages) == 1  # merged, NOT duplicated
    read = await wiki.get(pages[0].file_path)
    assert "step one" in read.content
    assert "episode #11" in read.content  # evolved with the new insight
