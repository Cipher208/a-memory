"""A1.2: wiki lifecycle status — frontmatter passthrough + search/list filter."""

import asyncio

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic_wiki(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    from wiki.manager import WikiManager

    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"))
    yield wm, tmp_path
    connection_manager._conns.clear()


async def test_status_frontmatter_roundtrip(hermetic_wiki):
    """status: in frontmatter survives create → read → update (was silently dropped)."""
    wm, _ = hermetic_wiki
    path = await wm.add(wiki_type="work_notes", title="status page", content="body", status="active")
    entry = await wm.get(path)
    assert entry.status == "active"

    await wm.update(path, content="new body", status="stale")
    entry2 = await wm.get(path)
    assert entry2.status == "stale"


async def test_status_defaults_active(hermetic_wiki):
    wm, _ = hermetic_wiki
    path = await wm.add(wiki_type="work_notes", title="no status page", content="b")
    entry = await wm.get(path)
    assert entry.status == "active"


async def test_search_filters_by_status(hermetic_wiki):
    wm, _ = hermetic_wiki
    await wm.add(wiki_type="work_notes", title="alpha live", content="findme alpha", status="active")
    await wm.add(wiki_type="work_notes", title="beta stale", content="findme beta", status="stale")
    await wm.add(wiki_type="work_notes", title="gamma archived", content="findme gamma", status="archived")

    hits = await wm.search("findme")
    titles = {e["title"] for e in hits}
    assert "alpha live" in titles
    assert "beta stale" not in titles  # default search = active only
    assert "gamma archived" not in titles

    all_hits = await wm.search("findme", status=None)
    assert {e["title"] for e in all_hits} == {"alpha live", "beta stale", "gamma archived"}

    listed = await wm.list_by_type("work_notes", status="stale")
    assert [e.title for e in listed] == ["beta stale"]


async def test_retired_implies_archived(hermetic_wiki):
    """retire() keeps working — archived pages drop out of default search."""
    wm, _ = hermetic_wiki
    path = await wm.add(wiki_type="work_notes", title="doomed page", content="doomed body")
    await wm.retire(path)
    hits = await wm.search("doomed body")
    assert all(e.title != "doomed page" for e in hits)
