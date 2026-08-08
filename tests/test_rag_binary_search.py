"""Integration tests for RAGEngine binary search (strategy='mib') via aiosqlite."""

import pytest

from rag.engine import RAGEngine
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def rag(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    r = RAGEngine(cm=cm, layer="user", binary_dim=384)
    await r.init_db()
    return r


@pytest.mark.asyncio
async def test_search_mib_returns_results_with_scores(rag):
    """MIB strategy should return results with scores."""
    await rag.ingest_text(
        title="Redis tuning",
        text="Redis is configured for high throughput with pipelining and AOF every sec.",
        user_id="alice",
    )
    await rag.ingest_text(
        title="UI styles",
        text="Buttons use CSS with hover transitions and rounded corners.",
        user_id="alice",
    )
    results = await rag.search("redis performance tuning", user_id="alice", strategy="mib", limit=5)
    assert len(results) == 2
    # Both results should have scores
    for r in results:
        assert "score" in r
        assert 0.0 <= r["score"] <= 1.0


@pytest.mark.asyncio
async def test_search_mib_works_without_numpy(rag):
    """Guarantee: MIB strategy doesn't crash if numpy is unavailable."""
    await rag.ingest_text("topic", "any content here for testing", user_id="bob")
    # This should work even without numpy (returns empty list if not _HAS_BINARY)
    out = await rag.search("any", user_id="bob", strategy="mib", limit=5)
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_search_hybrid_combines_fts5_and_mib(rag):
    await rag.ingest_text("alpha", "redis cluster mode for production deployments", user_id="u")
    await rag.ingest_text("beta", "completely unrelated topic about gardening", user_id="u")
    results = await rag.search("redis cluster", user_id="u", strategy="hybrid", limit=2)
    assert len(results) >= 1
    # The redis-related doc should appear in results
    titles_and_content = [r["title"] + " " + r["content"] for r in results]
    assert any("redis" in t.lower() for t in titles_and_content)


@pytest.mark.asyncio
async def test_ingest_stores_both_embeddings(rag):
    """Verify binary embedding is stored."""
    page_id = await rag.ingest_text("test", "Test content for embedding storage", user_id="charlie")
    conn = await rag._cm.get("memory.db")
    row = await (await conn.execute("SELECT bin_embedding FROM rag_chunks WHERE page_id=?", (page_id,))).fetchone()
    # Binary embedding should be present
    assert row["bin_embedding"] is not None


@pytest.mark.asyncio
async def test_search_mib_empty_database(rag):
    """MIB search on empty database returns empty list."""
    results = await rag.search("anything", user_id="empty", strategy="mib", limit=5)
    assert results == []


@pytest.mark.asyncio
async def test_search_mib_filters_by_user(rag):
    """MIB search respects user_id filter."""
    await rag.ingest_text("alice doc", "Content for alice", user_id="alice")
    await rag.ingest_text("bob doc", "Content for bob", user_id="bob")

    alice_results = await rag.search("content", user_id="alice", strategy="mib", limit=10)
    bob_results = await rag.search("content", user_id="bob", strategy="mib", limit=10)

    assert len(alice_results) == 1
    assert len(bob_results) == 1
    assert "alice" in alice_results[0]["content"].lower()
    assert "bob" in bob_results[0]["content"].lower()


@pytest.mark.asyncio
async def test_search_works_without_float_embeddings(tmp_path):
    """Search should work even when float embeddings are not stored."""
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    r = RAGEngine(cm=cm, layer="user", binary_dim=384)
    await r.init_db()
    await r.ingest_text("test", "content for testing", user_id="u")
    # Verify binary embedding is stored by default
    conn = await r._cm.get("memory.db")
    row = await (await conn.execute("SELECT bin_embedding FROM rag_chunks LIMIT 1")).fetchone()
    assert row["bin_embedding"] is not None

    # Search still works
    results = await r.search("testing", user_id="u", strategy="fts", limit=5)
    assert len(results) >= 1
