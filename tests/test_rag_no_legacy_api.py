"""Verify deprecated RAG methods are removed and don't appear via shadow paths."""

import pytest

from rag.engine import RAGEngine


def test_engine_does_not_expose_legacy_methods():
    """Deprecated search_rrf, search_its, search_binary must not exist."""
    e = RAGEngine()
    for name in ("search_rrf", "search_its", "search_binary"):
        assert not hasattr(e, name), f"{name}() should be deleted, but RAGEngine still has it"


def test_facade_public_methods_only():
    """Public API = only search() with strategy param."""
    expected_public = {"search", "ingest_text", "ingest_file", "init_db", "get_relations", "add_relation", "count_pages", "count_chunks"}
    actual_public = {m for m in dir(RAGEngine) if not m.startswith("_") and callable(getattr(RAGEngine, m, None))}
    extras = actual_public - expected_public
    # No search_* methods should remain (except 'search' itself)
    search_methods = {m for m in actual_public if m.startswith("search_")}
    assert search_methods == set(), f"unexpected search_* methods leaked: {search_methods}"


@pytest.mark.asyncio
async def test_search_uniformly_handles_three_strategies(tmp_path):
    """One entry, three strategies — no legacy wrappers needed."""
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    e = RAGEngine(cm=cm, binary_dim=8)
    await e.init_db()
    await e.ingest_text("a", "redis cluster pipelining", user_id="u")

    out = await e.search("redis", strategy="fts", user_id="u")
    out2 = await e.search("redis", strategy="mib", user_id="u")
    out3 = await e.search("redis", strategy="hybrid", user_id="u")
    assert len(out) and len(out2) and len(out3)
