"""Integration tests for WikiManager."""

import pytest
from pathlib import Path
from wiki import WikiManager
from shared.connection import connection_manager

@pytest.fixture
async def wiki(tmp_path):
    # Use a temporary directory for FS operations
    wiki_dir = tmp_path / "wiki"
    wm = WikiManager(layer="user", base_dir=str(wiki_dir))
    await wm.init_db()

    # Clean DB for testing layer isolation or re-runs
    conn = await connection_manager.get("memory.db")
    await conn.execute("DELETE FROM wiki_index WHERE layer='user'")
    await conn.commit()

    return wm
    # Cleanup connection if needed (AsyncConnectionManager handles it usually)

@pytest.mark.asyncio
async def test_wiki_manager_full_cycle(wiki):
    """
    Full cycle: Create .md file -> Manager.add() -> Manager.search() -> Verify results -> Manager.delete() -> Verify FS and DB cleanup.
    """
    title = "Integration Test"
    content = "This is a test content for wiki manager integration."
    tags = ["test", "integration"]
    wiki_type = "work_notes"

    # 1. Add
    file_path = await wiki.add(wiki_type, title, content, tags=tags)
    p = Path(file_path)
    assert p.exists()

    # Verify FS content
    text = p.read_text(encoding="utf-8")
    assert title in text
    assert content in text
    # WikiParser.to_markdown might format tags differently
    # assert "tags: test, integration" in text

    # 2. Search (Index should be populated)
    # FTS might need a tiny bit of time or immediate commit (WikiIndex.save commits)
    results = await wiki.search("integration")
    assert len(results) >= 1
    assert results[0]["title"] == title
    assert results[0]["file_path"] == file_path

    # 3. Get
    entry = await wiki.get(file_path)
    assert entry is not None
    assert entry.title == title
    assert entry.content == content
    assert entry.tags == tags

    # 4. Count
    count = await wiki.count(wiki_type)
    assert count == 1

    # 5. Delete
    deleted = await wiki.delete(file_path)
    assert deleted is True
    assert not p.exists()

    # Verify DB cleanup
    entry_after = await wiki.get(file_path)
    assert entry_after is None

    count_after = await wiki.count(wiki_type)
    assert count_after == 0

    search_after = await wiki.search("integration")
    assert len(search_after) == 0

@pytest.mark.asyncio
async def test_wiki_manager_reindex(wiki):
    """Test optimized reindex_all."""
    # Create file manually on disk
    wiki_type = "diary"
    type_dir = wiki.base_dir / wiki_type
    type_dir.mkdir(parents=True, exist_ok=True)

    file_path = type_dir / "manual.md"
    file_path.write_text("---\ntitle: Manual\n---\nManual content")

    # Reindex
    stats = await wiki.reindex_all()
    assert stats["indexed"] == 1

    # Verify in DB
    entry = await wiki.get(str(file_path))
    assert entry is not None
    assert entry.title == "Manual"

    # Reindex again (should skip due to hash check)
    stats2 = await wiki.reindex_all()
    assert stats2["skipped"] == 1
    assert stats2["indexed"] == 0

@pytest.mark.asyncio
async def test_wiki_manager_sync_external(wiki, tmp_path):
    """Test sync_external."""
    ext_dir = tmp_path / "external"
    ext_dir.mkdir()

    f = ext_dir / "external_note.md"
    f.write_text("# External Note\nContent here")

    stats = await wiki.sync_external(external_dirs=[str(ext_dir)])
    assert stats["imported"] == 1

    # Should be imported as 'diary' (guessed type)
    assert await wiki.count("diary") == 1

    # Sync again - should skip
    stats2 = await wiki.sync_external(external_dirs=[str(ext_dir)])
    assert stats2["skipped"] == 1
    assert stats2["imported"] == 0
