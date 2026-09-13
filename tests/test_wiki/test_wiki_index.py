import pytest
import time
import hashlib
from wiki import WikiIndex, WikiEntry
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def connection_manager(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    yield cm
    await cm.close_all()


@pytest.fixture
async def wiki_index(connection_manager):
    idx = WikiIndex(connection_manager, layer="test_layer")
    await idx.init_db()
    return idx


@pytest.mark.asyncio
async def test_init_db(wiki_index, connection_manager):
    assert await connection_manager.table_exists("memory.db", "wiki_index")
    assert await connection_manager.table_exists("memory.db", "wiki_fts")


@pytest.mark.asyncio
async def test_crud_operations(wiki_index):
    entry = WikiEntry(
        wiki_type="notes",
        title="Test Title",
        content="Test Content",
        file_path="/tmp/test.md",
        tags=["tag1", "tag2"],
        importance=0.8,
        created_at=time.time(),
        updated_at=time.time(),
    )
    content_hash = hashlib.sha256(entry.content.encode()).hexdigest()

    # Save
    await wiki_index.save(entry, content_hash)
    assert await wiki_index.count() == 1

    # Get by path
    row = await wiki_index.get_by_path(entry.file_path)
    assert row is not None
    assert row["title"] == entry.title
    assert row["content_hash"] == content_hash

    # Update
    entry.content = "Updated Content"
    new_hash = hashlib.sha256(entry.content.encode()).hexdigest()
    await wiki_index.save(entry, new_hash)

    row = await wiki_index.get_by_path(entry.file_path)
    assert row["content"] == "Updated Content"
    assert row["content_hash"] == new_hash

    # Count by type
    assert await wiki_index.count(wiki_type="notes") == 1
    assert await wiki_index.count(wiki_type="other") == 0

    # Delete
    await wiki_index.delete(entry.file_path)
    assert await wiki_index.count() == 0
    assert await wiki_index.get_by_path(entry.file_path) is None


@pytest.mark.asyncio
async def test_fts_search(wiki_index):
    entries = [
        WikiEntry(
            wiki_type="notes",
            title="Apple Pie",
            content="Recipe for a delicious apple pie.",
            file_path="/tmp/apple.md",
            created_at=time.time(),
            updated_at=time.time(),
        ),
        WikiEntry(
            wiki_type="notes",
            title="Banana Bread",
            content="How to make sweet banana bread.",
            file_path="/tmp/banana.md",
            created_at=time.time(),
            updated_at=time.time(),
        ),
    ]

    for entry in entries:
        h = hashlib.sha256(entry.content.encode()).hexdigest()
        await wiki_index.save(entry, h)

    # Search title
    results = await wiki_index.search("Apple")
    assert len(results) == 1
    assert results[0]["title"] == "Apple Pie"

    # Search content
    results = await wiki_index.search("delicious")
    assert len(results) == 1
    assert results[0]["title"] == "Apple Pie"

    # Partial match (using FTS5 syntax if enabled, but simple word match first)
    results = await wiki_index.search("banana")
    assert len(results) == 1
    assert results[0]["title"] == "Banana Bread"

    # Multiple matches
    results = await wiki_index.search("bread OR pie")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_fts_update_cleanup(wiki_index):
    entry = WikiEntry(
        wiki_type="notes",
        title="Apple",
        content="I like eating an apple.",
        file_path="/tmp/test_update.md",
        created_at=time.time(),
        updated_at=time.time(),
    )
    h1 = hashlib.sha256(entry.content.encode()).hexdigest()
    await wiki_index.save(entry, h1)

    # Verify initially searchable
    assert len(await wiki_index.search("Apple")) == 1

    # Update to Banana
    entry.title = "Banana"
    entry.content = "I prefer a banana."
    h2 = hashlib.sha256(entry.content.encode()).hexdigest()
    await wiki_index.save(entry, h2)

    # Verify old content is gone from index
    results_apple = await wiki_index.search("Apple")
    assert len(results_apple) == 0, f"Found 'Apple' in results after update: {results_apple}"

    # Verify new content is searchable
    results_banana = await wiki_index.search("Banana")
    assert len(results_banana) == 1
    assert results_banana[0]["title"] == "Banana"


@pytest.mark.asyncio
async def test_search_punctuation_query_still_finds(wiki_index):
    """FTS5 special chars must not drop real hits (comma crash, 2026-09-13 live recall)."""
    entry = WikiEntry(
        wiki_type="notes",
        title="Greeting",
        content="Привет мам эксперимент закрыт",
        file_path="/tmp/greet.md",
        created_at=time.time(),
        updated_at=time.time(),
    )
    await wiki_index.save(entry, hashlib.sha256(entry.content.encode()).hexdigest())
    results = await wiki_index.search("Привет, мам")
    assert [r["title"] for r in results] == ["Greeting"]
    # AND semantics preserved: an absent term still filters the page out.
    assert await wiki_index.search("Привет, отсутствующее") == []


@pytest.mark.asyncio
async def test_search_special_chars_no_error_log(wiki_index, caplog):
    """Operator-only queries yield no tokens: return [] early, no FTS5 exception."""
    assert await wiki_index.search('""" ( ) * ?? ,,,') == []
    assert "Search failed" not in caplog.text
