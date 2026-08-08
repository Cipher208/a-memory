"""Tests for wiki/ module (WikiManager) — async."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_file_wiki_add_search():
    from wiki import WikiManager

    async def t():
        w = WikiManager(layer="user")
        path = await w.add("work_notes", "Test Entry 2026", "Test content here", tags=["test"])
        assert path is not None
        assert Path(path).exists()
        results = await w.search("Test Entry 2026")
        assert len(results) > 0

    asyncio.run(t())


def test_file_wiki_enabled_types():
    from wiki import WikiManager

    w = WikiManager(layer="user")
    types = w.get_enabled_types()
    assert len(types) > 0
    assert "diary" in types


def test_file_wiki_count():
    from wiki import WikiManager

    async def t():
        w = WikiManager(layer="user")
        await w.add("work_notes", "Count Test 2026", "content")
        assert await w.count() >= 1

    asyncio.run(t())
