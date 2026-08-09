"""
Critical integration tests — the 15 tests that verify real module interactions.
Non-critical tests removed (covered by test_tools_e2e.py or unit tests).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
async def run_migrations():
    from shared.migrations import migration_manager

    await migration_manager.migrate()


@pytest.fixture
async def mm():
    from core import memory_manager

    return memory_manager


# ═══════════════════════════════════════════════════════════════
# CRITICAL PATHS — User + Agent (parametrized)
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["user", "agent"])
async def test_remember_recall(mm, layer):
    mem = mm.user_memory("test_integ") if layer == "user" else mm.agent_memory("test_integ")
    entry_id = await mem.remember("lang", "Python", 0.8)
    assert entry_id > 0
    results = await mem.recall("Python")
    assert len(results) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["user", "agent"])
async def test_forget(mm, layer):
    mem = mm.user_memory("test_integ") if layer == "user" else mm.agent_memory("test_integ")
    await mem.remember("temp_key", "temp_value", 0.5)
    deleted = await mem.forget("temp_key")
    assert deleted is True


# ═══════════════════════════════════════════════════════════════
# RAG — ingest, search, relations, conflict, router
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rag_ingest_search():
    from rag.engine import RAGEngine
    from shared.connection import connection_manager

    rag = RAGEngine(cm=connection_manager)
    await rag.init_db()
    await rag.ingest_text("Python Tips", "Use type hints", user_id="test_integ")
    results = await rag.search("type hints", user_id="test_integ")
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_rag_relations():
    from rag.engine import RAGEngine
    from shared.connection import connection_manager

    rag = RAGEngine(cm=connection_manager)
    await rag.init_db()
    page_id = await rag.ingest_text("Page A", "Content A", user_id="test_integ")
    page_id2 = await rag.ingest_text("Page B", "Content B", user_id="test_integ")
    await rag.add_relation(page_id, page_id2, "elaborates", 0.8)
    relations = await rag.get_relations(page_id)
    assert len(relations) >= 1


@pytest.mark.asyncio
async def test_conflict_resolver():
    from rag.conflict import ConflictResolver
    from shared.connection import connection_manager

    cr = ConflictResolver(cm=connection_manager)
    result = await cr.check("test_integ", "Python is great")
    assert result["is_conflict"] is False


@pytest.mark.asyncio
async def test_retrieval_router():
    from rag.router import RetrievalRouter

    router = RetrievalRouter(user_id="test_integ")
    result = await router.route("How to use Python?")
    assert hasattr(result, "strategy")


# ═══════════════════════════════════════════════════════════════
# SAGA + MIDDLEWARE — critical shared infrastructure
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_saga():
    from shared.saga import Saga

    saga = Saga("test_saga")
    saga.add_step("step1", lambda d: {"ok": True})
    result = await saga.execute()
    assert result is not None


@pytest.mark.asyncio
async def test_middleware():
    from shared.middleware import MiddlewareContext, MiddlewarePipeline

    pipeline = MiddlewarePipeline()
    ctx = MiddlewareContext(user_id="test_integ")
    result = await pipeline.execute(ctx, lambda c: {"ok": True})
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_embeddings():
    from shared.embeddings import EmbeddingCache

    cache = EmbeddingCache()
    emb = await cache.embed_single("Hello world")
    assert isinstance(emb, list)
    assert len(emb) > 0


# ═══════════════════════════════════════════════════════════════
# INFRASTRUCTURE — migrations, connection, dashboard
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_migrations():
    from shared.migrations import MigrationManager

    mm = MigrationManager()
    version = await mm.get_current_version()
    assert version is not None
    assert isinstance(version, str)


@pytest.mark.asyncio
async def test_connection_manager():
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager()
    conn = await cm.get("test_integ.db")
    assert conn is not None
    stats = cm.stats()
    assert stats["connections"] >= 1
    await cm.close_all()


@pytest.mark.asyncio
async def test_dashboard():
    from features.dashboard import Dashboard

    d = Dashboard()
    stats = await d.get_stats("test_integ")
    assert isinstance(stats, dict)
    assert "l1_buffer" in stats
    assert "l4_facts" in stats


@pytest.mark.asyncio
async def test_metrics():
    from shared.metrics import metrics

    metrics.inc("test_counter")
    metrics.gauge("test_gauge", 1.0)
    json_out = metrics.render_json()
    assert "counters" in json_out
    assert "test_counter" in json_out["counters"]
