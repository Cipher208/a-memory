"""D2.2: store pipeline — promote distilled episodes into skill pages."""

import time
from types import SimpleNamespace

import pytest


class _FakeL3:
    def __init__(self, episodes):
        self._eps = {e.episode_id: e for e in episodes}

    async def get_by_id(self, episode_id):
        return self._eps.get(episode_id)

    async def search_by_tag(self, user_id, tag, limit=10):
        return [e for e in self._eps.values() if tag in (e.tags or [])]

    async def add_tag(self, episode_id, tag):
        ep = self._eps.get(episode_id)
        if ep is None:
            return False
        if tag not in (ep.tags or []):
            ep.tags = [*list(ep.tags or []), tag]
        return True


class _FakeWiki:
    def __init__(self):
        self.pages = []

    async def add(self, wiki_type, title, content, tags=None, importance=0.5):
        path = f"{wiki_type}/{title.replace(' ', '_')}.md"
        self.pages.append({"type": wiki_type, "title": title, "content": content, "path": path})
        return path


class _FakeMem:
    def __init__(self, episodes):
        self.l3 = _FakeL3(episodes)


def _ep(eid, summary, tags=None, age_s=0.0):
    return SimpleNamespace(
        episode_id=eid,
        summary=summary,
        tags=list(tags or []),
        created_at=time.time() - age_s,
    )


@pytest.mark.asyncio
async def test_promote_creates_skill_page_and_tags():
    from features.skill_pipeline import promote_episodes

    wiki = _FakeWiki()
    mem = _FakeMem([_ep(7, "Deploy ariel: ssh vm1282008, uv sync, restart units")])
    res = await promote_episodes(mem, wiki, "u1", [7])
    assert res["count"] == 1 and not res["skipped"]
    page = wiki.pages[0]
    assert page["type"] == "skill"
    assert "episode #7" in page["content"]
    assert "skill_promoted" in mem.l3._eps[7].tags


@pytest.mark.asyncio
async def test_promote_idempotent_and_missing():
    from features.skill_pipeline import promote_episodes

    wiki = _FakeWiki()
    mem = _FakeMem([_ep(7, "some skill", tags=["skill_promoted"]), _ep(8, "x")])
    res = await promote_episodes(mem, wiki, "u1", [7, 8, 99])
    assert res["count"] == 1  # only 8 promoted
    reasons = {s["episode_id"]: s["reason"] for s in res["skipped"]}
    assert reasons[7] == "already_promoted" and reasons[99] == "not_found"
    assert len(wiki.pages) == 1


@pytest.mark.asyncio
async def test_auto_promote_fresh_window():
    from features.skill_pipeline import auto_promote_fresh

    wiki = _FakeWiki()
    mem = _FakeMem(
        [
            _ep(1, "fresh skill", tags=["dream_skill"], age_s=3600),
            _ep(2, "stale skill", tags=["dream_skill"], age_s=30 * 86400),
        ]
    )
    res = await auto_promote_fresh(mem, wiki, "u1", days=7)
    assert res["promoted"] == 1
    assert wiki.pages[0]["title"].startswith("fresh skill")


@pytest.mark.asyncio
async def test_auto_promote_empty():
    from features.skill_pipeline import auto_promote_fresh

    res = await auto_promote_fresh(_FakeMem([]), _FakeWiki(), "u1")
    assert res == {"promoted": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_episodic_add_tag_and_get_by_id_real_db(tmp_path, monkeypatch):
    """Real SQL path for the promotion primitives (fakes mirror these)."""
    from shared.connection import connection_manager

    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    try:
        from core.episodic import EpisodicMemory

        em = EpisodicMemory(layer="user")
        await em._init_db()
        eid = await em.save("u1", "draft skill content", tags=["dream_skill"])
        ep = await em.get_by_id(eid)
        assert ep is not None and ep.summary == "draft skill content"
        assert await em.add_tag(eid, "skill_promoted") is True
        assert await em.add_tag(eid, "skill_promoted") is True  # idempotent
        ep2 = await em.get_by_id(eid)
        assert ep2 is not None and "skill_promoted" in ep2.tags and "dream_skill" in ep2.tags
        assert await em.add_tag(999, "x") is False
    finally:
        connection_manager.base_dir = original
