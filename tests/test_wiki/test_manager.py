"""Tests for WikiManager — unified wiki with layer separation."""

import pytest

from wiki import WikiManager


@pytest.fixture
async def user_wiki(tmp_path):
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki"))
    await wm.init_db()
    # Clean entries from previous tests (FTS5 content tables auto-sync via triggers)
    conn = await wm._cm.get("memory.db")
    await conn.execute("DELETE FROM wiki_index WHERE layer='user'")
    await conn.commit()
    return wm


@pytest.fixture
async def agent_wiki(tmp_path):
    wm = WikiManager(layer="agent", base_dir=str(tmp_path / "wiki"))
    await wm.init_db()
    # Clean entries from previous tests
    conn = await wm._cm.get("memory.db")
    await conn.execute("DELETE FROM wiki_index WHERE layer='agent'")
    await conn.commit()
    return wm


@pytest.mark.asyncio
async def test_user_wiki_add_and_get(user_wiki):
    path = await user_wiki.add("diary", "Test Entry", "Some content", tags=["test"])
    assert path.endswith(".md")
    entry = await user_wiki.get(path)
    assert entry is not None
    assert entry.title == "Test Entry"


@pytest.mark.asyncio
async def test_agent_wiki_add_and_get(agent_wiki):
    path = await agent_wiki.add("decision_log", "Decision", "Chose X", tags=["arch"])
    entry = await agent_wiki.get(path)
    assert entry is not None
    assert entry.title == "Decision"


@pytest.mark.asyncio
async def test_wiki_type_isolation(user_wiki, agent_wiki):
    """User and agent wikis use different layers."""
    await user_wiki.add("diary", "User Note", "content")
    await agent_wiki.add("decision_log", "Agent Note", "content")
    assert await user_wiki.count() == 1
    assert await agent_wiki.count() == 1


@pytest.mark.asyncio
async def test_wiki_disabled_type_raises(user_wiki):
    with pytest.raises(ValueError, match="disabled"):
        await user_wiki.add("nonexistent_type", "Title", "Content")


@pytest.mark.asyncio
async def test_wiki_update(user_wiki):
    path = await user_wiki.add("diary", "Original", "content")
    await user_wiki.update(path, title="Updated")
    entry = await user_wiki.get(path)
    assert entry.title == "Updated"


@pytest.mark.asyncio
async def test_wiki_delete(user_wiki):
    path = await user_wiki.add("diary", "ToDelete", "content")
    result = await user_wiki.delete(path)
    assert result is True
    entry = await user_wiki.get(path)
    assert entry is None


@pytest.mark.asyncio
async def test_wiki_search(user_wiki):
    await user_wiki.add("diary", "Python Learning", "Learned async/await today")
    await user_wiki.add("diary", "Grocery Shopping", "Bought milk and eggs")
    results = await user_wiki.search("Python")
    assert len(results) >= 1
    assert any("Python" in r["title"] for r in results)


@pytest.mark.asyncio
async def test_wiki_list_by_type(user_wiki):
    await user_wiki.add("diary", "Entry 1", "content")
    await user_wiki.add("diary", "Entry 2", "content")
    entries = await user_wiki.list_by_type("diary")
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_wiki_list_all(user_wiki):
    await user_wiki.add("diary", "Entry 1", "content")
    await user_wiki.add("relationships", "Friend", "Best friend")
    entries = await user_wiki.list_all()
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_wiki_count(user_wiki):
    await user_wiki.add("diary", "Entry 1", "content")
    await user_wiki.add("diary", "Entry 2", "content")
    assert await user_wiki.count() == 2
    assert await user_wiki.count("diary") == 2
    assert await user_wiki.count("relationships") == 0


@pytest.mark.asyncio
async def test_wiki_rejects_traversal(user_wiki):
    with pytest.raises(ValueError, match="escapes base directory"):
        await user_wiki.update("../../etc/passwd", content="pwned")


def test_wiki_enabled_types(user_wiki):
    types = user_wiki.get_enabled_types()
    assert "diary" in types
    assert "relationships" in types


def test_wiki_agent_enabled_types(agent_wiki):
    types = agent_wiki.get_enabled_types()
    assert "decision_log" in types
    assert "error_analysis" in types
