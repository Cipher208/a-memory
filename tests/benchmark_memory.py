"""Benchmark memory operations."""

import asyncio
import os
import time

os.environ["MCP_MASTER_KEY"] = "benchmark-test-key"
from features import secrets

secrets._master_cache.clear()


def benchmark_remember(n: int = 100):
    from core import MemoryManager
    from shared.cache import MemoryCache

    mm = MemoryManager(cache=MemoryCache())

    async def run():
        start = time.perf_counter()
        for i in range(n):
            await mm.user_memory("bench_user").remember(f"key_{i}", f"value_{i}", 0.5)
        return time.perf_counter() - start

    return asyncio.run(run())


def benchmark_recall(n: int = 100):
    from core import MemoryManager
    from shared.cache import MemoryCache

    mm = MemoryManager(cache=MemoryCache())

    async def setup():
        for i in range(n):
            await mm.user_memory("bench_user").remember(f"key_{i}", f"value_{i}", 0.5)

    asyncio.run(setup())

    async def run():
        start = time.perf_counter()
        for _ in range(10):
            await mm.user_memory("bench_user").recall("key", 10)
        return time.perf_counter() - start

    return asyncio.run(run())


def benchmark_encrypt(n: int = 100):
    from features.secrets import decrypt_json, encrypt_json

    data = {"key": "value", "number": 42, "list": [1, 2, 3]}

    start = time.perf_counter()
    for _ in range(n):
        blob = encrypt_json(data)
        decrypt_json(blob)
    return time.perf_counter() - start


if __name__ == "__main__":
    benchmark_remember(100)
    benchmark_recall(100)
    benchmark_encrypt(100)
