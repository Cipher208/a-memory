import pytest
from rag.engine import RAGEngine
from shared.connection import connection_manager
from shared.constants import DB_NAME


@pytest.mark.asyncio
async def test_rag_engine_lifecycle():
    # Use a temporary database for testing if possible, or just ensure cleanup
    engine = RAGEngine(layer="test_layer")
    await engine.init_db()

    user_id = "test_user"
    title = "Test Page"
    content = "This is a test content about artificial intelligence and neural networks. It should be long enough to be interesting for search."

    # 1. Ingest text
    page_id = await engine.ingest_text(title, content, user_id=user_id, wiki_type="test")
    assert page_id > 0

    # Verify ingestion
    count = await engine.count_pages(user_id)
    assert count >= 1

    # 2. Search for the content
    # We use a delay or ensure commit is done (ingest_text handles commit)
    results = await engine.search("artificial intelligence", user_id=user_id, strategy="fts")
    assert len(results) > 0
    assert results[0]["title"] == title
    assert "artificial intelligence" in results[0]["content"].lower()

    # 3. Test Relations
    page_id_2 = await engine.ingest_text("Related Page", "More content here.", user_id=user_id)
    await engine.add_relation(page_id, page_id_2, relation_type="tests")

    relations = await engine.get_relations(page_id)
    assert len(relations) == 1
    assert relations[0]["id"] == page_id_2
    assert relations[0]["relation"] == "tests"

    # Cleanup (optional, depends on how DB_NAME is configured in tests)
    # If it's a real file, we might want to delete the test entries
    conn = await connection_manager.get(DB_NAME)
    await conn.execute("DELETE FROM rag_pages WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM rag_chunks WHERE page_id IN (?, ?)", (page_id, page_id_2))
    await conn.execute("DELETE FROM rag_relations WHERE source_id = ?", (page_id,))
    await conn.commit()
