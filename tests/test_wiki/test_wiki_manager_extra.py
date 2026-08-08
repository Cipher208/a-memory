import pytest
import asyncio
from wiki import WikiManager

@pytest.fixture
async def wiki(tmp_path):
    # Ensure base_dir exists
    base_dir = tmp_path / "wiki"
    base_dir.mkdir(parents=True, exist_ok=True)
    wm = WikiManager(layer="user", base_dir=str(base_dir))
    await wm.init_db()
    
    # Clean entries from previous tests
    conn = await wm._cm.get("memory.db")
    await conn.execute("DELETE FROM wiki_index WHERE layer='user'")
    await conn.commit()
    
    return wm

@pytest.mark.asyncio
async def test_wiki_concurrent_adds(wiki):
    """Verify that multiple concurrent additions work (SQLite WAL mode)."""
    tasks = []
    for i in range(10):
        tasks.append(wiki.add("diary", f"Title {i}", f"Content {i}"))
    
    paths = await asyncio.gather(*tasks)
    assert len(paths) == 10
    
    count = await wiki.count()
    assert count == 10

@pytest.mark.asyncio
async def test_wiki_reindex_idempotency(wiki):
    """Verify that reindexing the same files doesn't create duplicates."""
    await wiki.add("diary", "Entry 1", "Content 1")
    await wiki.add("diary", "Entry 2", "Content 2")
    
    initial_count = await wiki.count()
    assert initial_count == 2
    
    # Reindex
    result = await wiki.reindex_all()
    assert result["skipped"] == 2
    assert await wiki.count() == 2

@pytest.mark.asyncio
async def test_wiki_search_fts_special_chars(wiki):
    """Verify search handles special FTS5 characters or ignores them gracefully."""
    await wiki.add("diary", "Special Char", "Content with * and ? and ^")
    
    results = await wiki.search("*") 
    results2 = await wiki.search("?")
    
    assert isinstance(results, list)
    assert isinstance(results2, list)

@pytest.mark.asyncio
async def test_wiki_update_nonexistent(wiki):
    """Verify update on non-existent file returns None (implied by no return)."""
    res = await wiki.update("ghost.md", title="New Title")
    # Current implementation returns None (no return statement)
    assert res is None
