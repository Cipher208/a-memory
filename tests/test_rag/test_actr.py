"""ACT-R base-level activation — pure function + CoreMemory.search contract."""

import time

from rag.actr import actr_activation


def test_activation_bounds():
    now = time.time()
    assert actr_activation(now, now, 0) == 0.0
    assert actr_activation(now, now - 86400 * 365, 1) == 0.0  # year-old single access
    a = actr_activation(now, now, 1)
    assert 0.0 < a < 1.0
    assert actr_activation(now, now, 50) > a  # frequency raises activation
    assert actr_activation(now, now - 3600, 1) > actr_activation(now, now - 86400 * 7, 1)  # recency


def test_core_search_carries_entry_id_and_updated_at(tmp_path):
    """D1.17 contract: search results carry entry_id + updated_at for activation."""
    import asyncio

    from core.memory import CoreMemory
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        core = CoreMemory(cm=cm, layer="user")
        await core._init_db()
        await core.save("u1", "alpha", "first fact", importance=0.9)
        await core.save("u1", "beta", "second fact", importance=0.5)
        facts = await core.search("u1", "fact")
        assert len(facts) == 2
        for f in facts:
            assert isinstance(f["entry_id"], int)
            assert isinstance(f["updated_at"], float)

    asyncio.run(t())
