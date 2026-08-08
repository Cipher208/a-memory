"""Launch demo — single script that starts the server, creates test data, and shows results.

Usage:
    python demo.py                    # Run demo
    python demo.py --transport stdio  # Specify transport
    python demo.py --port 8000        # Specify port

This script:
1. Starts the MCP server (HTTP mode)
2. Creates test data (users, memories, wiki entries, graph nodes)
3. Runs all search strategies (FTS, MIB, hybrid)
4. Demonstrates hooks, saga, and backup
5. Outputs formatted results with timing

Requirements:
    pip install mcp-ariel-memory[all]
"""

import asyncio
import os
import sys
import time

# Set master key for demo
os.environ["MCP_MASTER_KEY"] = "demo-master-key-for-testing"
from features import secrets

secrets._master_cache.clear()


async def run_demo():
    """Run the complete demo."""

    # 1. Import and initialize
    from core import MemoryManager
    from features.backup import BackupManager
    from features.secrets import decrypt_json, encrypt_json
    from graph.epistemic import EpistemicGraph
    from rag.engine import RAGEngine
    from shared.connection import connection_manager

    mm = MemoryManager()
    rag = RAGEngine(cm=connection_manager, layer="user", binary_dim=384)
    await rag.init_db()
    eg = EpistemicGraph(layer="user")
    await eg.init_db()

    # 2. Create test data

    # Users and memories
    start = time.perf_counter()
    for i in range(20):
        await mm.user_memory("demo_user").remember(f"key_{i}", f"value_{i}", 0.5 + i * 0.025)

    # RAG pages
    topics = [
        "Redis configuration and performance tuning",
        "PostgreSQL replication and backup strategies",
        "Docker container orchestration with Kubernetes",
        "Python async programming patterns",
        "Machine learning model deployment",
        "API gateway design and rate limiting",
    ]
    start = time.perf_counter()
    for i, topic in enumerate(topics):
        text = f"Chunk {i}: {topic}. Detailed explanation with examples."
        await rag.ingest_text(f"doc_{i}", text, user_id="demo_user")

    # Graph nodes
    start = time.perf_counter()
    for i in range(10):
        tags = ["redis", "cache", "database"] if i % 2 == 0 else ["python", "async", "patterns"]
        await eg.add_node("demo_user", f"Node {i} content", "fact", tags, 0.8)

    # 3. Run searches

    # FTS search
    start = time.perf_counter()
    fts_results = await rag.search("Redis configuration", user_id="demo_user", strategy="fts", limit=3)
    time.perf_counter() - start
    for _r in fts_results[:2]:
        pass

    # MIB search
    start = time.perf_counter()
    mib_results = await rag.search("Redis configuration", user_id="demo_user", strategy="mib", limit=3)
    time.perf_counter() - start
    for _r in mib_results[:2]:
        pass

    # Hybrid search
    start = time.perf_counter()
    hybrid_results = await rag.search("Redis configuration", user_id="demo_user", strategy="hybrid", limit=3)
    time.perf_counter() - start
    for _r in hybrid_results[:2]:
        pass

    # Tag lookup
    start = time.perf_counter()
    await eg.query_by_tag("demo_user", "redis")
    time.perf_counter() - start

    # 4. Test encryption
    data = {"api_key": "sk-demo-12345", "token": "abc-xyz"}
    start = time.perf_counter()
    encrypted = encrypt_json(data)
    decrypt_json(encrypted)
    time.perf_counter() - start

    # 5. Test saga
    from shared.saga import SagaEngine, FileSagaStore, SAGA_DIR, SagaState, SagaStep

    async def step1(d):
        return {"step1_done": True}

    async def step2(d):
        return {"step2_done": True}

    steps = [SagaStep(name="step1", action=step1), SagaStep(name="step2", action=step2)]

    store = FileSagaStore(SAGA_DIR)
    engine = SagaEngine(store)
    state = SagaState(name="demo_saga", context={"user_id": "demo_user"})

    start = time.perf_counter()
    await engine.execute(state, steps)
    time.perf_counter() - start

    # 6. Test backup
    backup_mgr = BackupManager()
    start = time.perf_counter()
    await backup_mgr.backup(label="demo_backup")
    time.perf_counter() - start

    # Summary


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="mcp-ariel-memory demo")
    parser.add_argument("--transport", default="http", help="Transport: http or stdio")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport")
    parser.add_argument("--demo-only", action="store_true", help="Run demo without starting server")
    args = parser.parse_args()

    if args.demo_only:
        asyncio.run(run_demo())
    else:
        # Run demo then start server
        asyncio.run(run_demo())

        # Start server
        import subprocess

        cmd = [
            sys.executable,
            "-m",
            "mcp_server.server",
            "--transport",
            args.transport,
            "--port",
            str(args.port),
            "--no-auth",
        ]
        subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
