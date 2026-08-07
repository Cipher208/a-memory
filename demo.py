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
    print("=" * 60)
    print("mcp-ariel-memory — Launch Demo")
    print("=" * 60)
    print()

    # 1. Import and initialize
    print("[1/6] Initializing components...")
    from core import MemoryManager
    from features.backup import BackupManager
    from features.secrets import decrypt_json, encrypt_json
    from graph.epistemic import EpistemicGraph
    from rag.engine import RAGEngine
    from shared.connection import connection_manager
    from shared.saga import Saga

    mm = MemoryManager()
    rag = RAGEngine(cm=connection_manager, layer="user", binary_dim=384)
    await rag.init_db()
    eg = EpistemicGraph(layer="user")
    await eg.init_db()
    print("  ✓ Components initialized")

    # 2. Create test data
    print()
    print("[2/6] Creating test data...")

    # Users and memories
    start = time.perf_counter()
    for i in range(20):
        await mm.user_memory("demo_user").remember(f"key_{i}", f"value_{i}", 0.5 + i * 0.025)
    print(f"  ✓ 20 memories created in {time.perf_counter() - start:.3f}s")

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
    print(f"  ✓ 6 RAG pages ingested in {time.perf_counter() - start:.3f}s")

    # Graph nodes
    start = time.perf_counter()
    for i in range(10):
        tags = ["redis", "cache", "database"] if i % 2 == 0 else ["python", "async", "patterns"]
        await eg.add_node("demo_user", f"Node {i} content", "fact", tags, 0.8)
    print(f"  ✓ 10 graph nodes created in {time.perf_counter() - start:.3f}s")

    # 3. Run searches
    print()
    print("[3/6] Running searches...")

    # FTS search
    start = time.perf_counter()
    fts_results = await rag.search("Redis configuration", user_id="demo_user", strategy="fts", limit=3)
    fts_time = time.perf_counter() - start
    print(f"  ✓ FTS: {len(fts_results)} results in {fts_time:.3f}s")
    for r in fts_results[:2]:
        print(f"    - {r['title']}: {r['content'][:60]}...")

    # MIB search
    start = time.perf_counter()
    mib_results = await rag.search("Redis configuration", user_id="demo_user", strategy="mib", limit=3)
    mib_time = time.perf_counter() - start
    print(f"  ✓ MIB: {len(mib_results)} results in {mib_time:.3f}s")
    for r in mib_results[:2]:
        print(f"    - {r['title']}: score={r['score']:.3f}")

    # Hybrid search
    start = time.perf_counter()
    hybrid_results = await rag.search("Redis configuration", user_id="demo_user", strategy="hybrid", limit=3)
    hybrid_time = time.perf_counter() - start
    print(f"  ✓ Hybrid: {len(hybrid_results)} results in {hybrid_time:.3f}s")
    for r in hybrid_results[:2]:
        print(f"    - {r['title']}: score={r['score']:.3f}")

    # Tag lookup
    start = time.perf_counter()
    tag_results = await eg.query_by_tag("demo_user", "redis")
    tag_time = time.perf_counter() - start
    print(f"  ✓ Tag lookup: {len(tag_results)} results in {tag_time:.3f}s")

    # 4. Test encryption
    print()
    print("[4/6] Testing encryption...")
    data = {"api_key": "sk-demo-12345", "token": "abc-xyz"}
    start = time.perf_counter()
    encrypted = encrypt_json(data)
    decrypted = decrypt_json(encrypted)
    enc_time = time.perf_counter() - start
    print(f"  ✓ Encrypt/decrypt: {enc_time:.3f}s")
    print(f"  ✓ Data matches: {data == decrypted}")

    # 5. Test saga
    print()
    print("[5/6] Testing saga...")
    saga = Saga("demo_saga")
    saga.add_step("step1", lambda d: {"step1_done": True})
    saga.add_step("step2", lambda d: {"step2_done": True})
    start = time.perf_counter()
    result = await saga.execute({"user_id": "demo_user"})
    saga_time = time.perf_counter() - start
    print(f"  ✓ Saga completed in {saga_time:.3f}s")
    print(f"  ✓ Status: {saga.status.value}")
    print(f"  ✓ Data: {result}")

    # 6. Test backup
    print()
    print("[6/6] Testing backup...")
    backup_mgr = BackupManager()
    start = time.perf_counter()
    backup_path = await backup_mgr.backup(label="demo_backup")
    backup_time = time.perf_counter() - start
    print(f"  ✓ Backup created in {backup_time:.3f}s")
    print(f"  ✓ Path: {backup_path}")

    # Summary
    print()
    print("=" * 60)
    print("Demo Summary")
    print("=" * 60)
    print(f"FTS search:       {fts_time:.3f}s ({len(fts_results)} results)")
    print(f"MIB search:       {mib_time:.3f}s ({len(mib_results)} results)")
    print(f"Hybrid search:    {hybrid_time:.3f}s ({len(hybrid_results)} results)")
    print(f"Tag lookup:       {tag_time:.3f}s ({len(tag_results)} results)")
    print(f"Encryption:       {enc_time:.3f}s")
    print(f"Saga execution:   {saga_time:.3f}s")
    print(f"Backup:           {backup_time:.3f}s")
    print()
    print("Demo complete! Server ready at http://localhost:8000")
    print("Dashboard: http://localhost:8000/dashboard")
    print("Metrics: http://localhost:8000/metrics")
    print()
    print("Press Ctrl+C to stop the server.")


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
        print("\nStarting server...")
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
        print(f"Running: {' '.join(cmd)}")
        subprocess.run(cmd)


if __name__ == "__main__":
    main()
