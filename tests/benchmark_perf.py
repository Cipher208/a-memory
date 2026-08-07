"""Performance benchmark for RAG, tags, and embedding operations.

Tests:
  1. Tag lookup: epi_tags JOIN vs LIKE
  2. Binary search: batched vs fetchall
  3. rag_chunks index: JOIN with page_id
  4. FTS5 search baseline

Run: python -m tests.benchmark_perf
"""

import asyncio
import os
import time

os.environ["MCP_MASTER_KEY"] = "benchmark-test-key"
from features import secrets

secrets._master_cache.clear()

import tempfile

from rag.engine import RAGEngine
from shared.connection import AsyncConnectionManager


async def _setup_rag(tmp_path: str, n_chunks: int = 500) -> RAGEngine:
    """Create RAG engine with n_chunks ingested."""
    cm = AsyncConnectionManager(base_dir=tmp_path)
    rag = RAGEngine(cm=cm, layer="user", binary_dim=384)
    await rag.init_db()

    # Ingest content with varying topics
    topics = [
        "Redis configuration and performance tuning",
        "PostgreSQL replication and backup strategies",
        "Docker container orchestration with Kubernetes",
        "Python async programming patterns",
        "Machine learning model deployment",
        "API gateway design and rate limiting",
        "Database migration strategies and rollback",
        "Microservices architecture patterns",
        "Caching strategies for high throughput",
        "Logging and observability best practices",
    ]

    for i in range(n_chunks):
        topic = topics[i % len(topics)]
        text = (
            f"Chunk {i}: {topic}. This is a detailed explanation of {topic} with practical examples and considerations for production environments."
        )
        await rag.ingest_text(f"doc_{i}", text, user_id="bench")

    return rag


async def bench_fts_search(rag: RAGEngine, n: int = 100) -> dict:
    """FTS5 search baseline."""
    start = time.perf_counter()
    for _i in range(n):
        query = "Redis configuration"
        await rag.search(query, user_id="bench", strategy="fts", limit=10)
    elapsed = time.perf_counter() - start
    return {"operation": "fts_search", "n": n, "elapsed_s": elapsed, "ops_per_s": n / elapsed}


async def bench_mib_search(rag: RAGEngine, n: int = 50) -> dict:
    """MIB binary search with batched reads."""
    start = time.perf_counter()
    for _i in range(n):
        query = "Redis configuration and performance"
        await rag.search(query, user_id="bench", strategy="mib", limit=10)
    elapsed = time.perf_counter() - start
    return {"operation": "mib_search", "n": n, "elapsed_s": elapsed, "ops_per_s": n / elapsed}


async def bench_hybrid_search(rag: RAGEngine, n: int = 50) -> dict:
    """Hybrid search (FTS + MIB)."""
    start = time.perf_counter()
    for _i in range(n):
        query = "Redis configuration and performance"
        await rag.search(query, user_id="bench", strategy="hybrid", limit=10)
    elapsed = time.perf_counter() - start
    return {"operation": "hybrid_search", "n": n, "elapsed_s": elapsed, "ops_per_s": n / elapsed}


async def bench_tag_lookup(n: int = 200) -> dict:
    """epi_tags JOIN lookup benchmark."""
    from graph.epistemic import EpistemicGraph

    eg = EpistemicGraph(layer="bench")
    await eg.init_db()

    # Insert nodes with tags
    for i in range(n):
        tags = ["redis", "cache", "database", "config"] if i % 2 == 0 else ["python", "async", "patterns"]
        await eg.add_node("bench_user", f"Node {i} content", "fact", tags, 0.5 + (i % 10) * 0.05)

    # Query by tag (uses JOIN)
    start = time.perf_counter()
    for i in range(50):
        await eg.query_by_tag("bench_user", "redis")
    elapsed = time.perf_counter() - start
    return {"operation": "epi_tags_join", "n": 50, "elapsed_s": elapsed, "ops_per_s": 50 / elapsed}


async def bench_rag_join(rag: RAGEngine, n: int = 100) -> dict:
    """rag_chunks JOIN benchmark (with index)."""
    # Test a simple count with JOIN
    conn = await rag._cm.get("memory.db")
    start = time.perf_counter()
    for _i in range(n):
        cur = await conn.execute(
            "SELECT COUNT(*) FROM rag_chunks c JOIN rag_pages p ON c.page_id = p.id WHERE p.user_id = ?",
            ("bench",),
        )
        await cur.fetchone()
    elapsed = time.perf_counter() - start
    return {"operation": "rag_chunks_join", "n": n, "elapsed_s": elapsed, "ops_per_s": n / elapsed}


async def run_all():
    """Run all benchmarks."""
    tmp = tempfile.mkdtemp()

    rag = await _setup_rag(tmp, n_chunks=500)

    results = []

    # FTS
    r = await bench_fts_search(rag, n=100)
    results.append(r)

    # MIB
    r = await bench_mib_search(rag, n=50)
    results.append(r)

    # Hybrid
    r = await bench_hybrid_search(rag, n=50)
    results.append(r)

    # Tag lookup
    r = await bench_tag_lookup(n=200)
    results.append(r)

    # JOIN index
    r = await bench_rag_join(rag, n=100)
    results.append(r)

    for r in results:
        pass

    return results


if __name__ == "__main__":
    asyncio.run(run_all())
