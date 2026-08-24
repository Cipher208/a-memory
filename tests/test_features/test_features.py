"""Tests for features/ module — unique tests only."""

import asyncio


def test_compression():
    from features.compression import MemoryCompressor

    async def t():
        mc = MemoryCompressor()
        stats = await mc.get_stats("test_feat")
        assert "core_memory" in stats

    asyncio.run(t())
