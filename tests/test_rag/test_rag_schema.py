import pytest
from rag.models import RAGPage, RAGChunk, SearchResult
from rag.schema import init_rag_db


@pytest.mark.asyncio
async def test_rag_models_validation():
    # Test RAGPage
    page = RAGPage(title="Test Page", content="Test Content")
    assert page.title == "Test Page"
    assert page.layer == "user"
    assert page.user_id == "default"
    assert page.created_at is not None

    # Test RAGChunk
    chunk = RAGChunk(page_id=1, chunk_index=0, content="Chunk Content")
    assert chunk.page_id == 1
    assert chunk.content == "Chunk Content"

    # Test SearchResult
    res = SearchResult(page_id=1, title="Test", content="Snippet", score=0.9)
    assert res.score == 0.9
    assert res.metadata == {}


@pytest.mark.asyncio
async def test_init_rag_db(tmp_path):
    # Use a temporary database for testing
    db_path = tmp_path / "test_rag.db"

    # We need to mock AsyncConnectionManager or use a real one with a test db
    # cm = AsyncConnectionManager()
    # Override DB_NAME for testing if possible, or just rely on cm's behavior
    # In this project, connection_manager is often used as a singleton.
    # For testing, we might need a more isolated approach.

    # Let's try to use a real connection and check schema
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        # Mocking the cm behavior for init_rag_db
        class MockCM:
            async def execute_script(self, name, script):
                await conn.executescript(script)

        await init_rag_db(MockCM(), fts_available=True)

        # Verify tables exist
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = [row[0] for row in await cursor.fetchall()]
            assert "rag_pages" in tables
            assert "rag_chunks" in tables
            assert "rag_relations" in tables
            assert "rag_fts" in tables

        # Verify indexes
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='index'") as cursor:
            indexes = [row[0] for row in await cursor.fetchall()]
            assert "idx_rag_user" in indexes
