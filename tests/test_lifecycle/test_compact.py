import time

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _insert(user_id: str, layer: str, rows: list[tuple[str, str, float, float]]) -> None:
    """rows: (key, kind, importance, updated_at)."""
    conn = await connection_manager.get("memory.db")
    await conn.executemany(
        "INSERT INTO core_memory (user_id, layer, key, value, importance, created_at, updated_at, memory_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(user_id, layer, k, f"v:{k}", imp, ts, ts, kind) for k, kind, imp, ts in rows],
    )
    await conn.commit()


async def _keys(user_id: str) -> list[str]:
    conn = await connection_manager.get("memory.db")
    rows = await (await conn.execute("SELECT key FROM core_memory WHERE user_id=?", (user_id,))).fetchall()
    return [r["key"] for r in rows]


@pytest.mark.asyncio
async def test_compact_under_budget(cm):
    from lifecycle.compact import compact_under_budget

    now = time.time()
    day = 86400.0
    rows = [(f"junk_{i}", "fact", 0.05, now - 200 * day) for i in range(100)]
    rows += [(f"mid_{i}", "fact", 0.5, now - day) for i in range(250)]
    rows += [(f"high_{i}", "fact", 0.9, now - day) for i in range(240)]
    rows += [(f"rule_{i}", "rule", 0.9, now - day) for i in range(5)]
    rows += [(f"instr_{i}", "instruction", 0.9, now - day) for i in range(3)]
    rows += [(f"commit_{i}", "commitment", 0.9, now - day) for i in range(2)]
    await _insert("u1", "user", rows)

    result = await compact_under_budget("u1", "user", budget=500)
    assert result["evicted"] == 100
    assert result["remaining"] <= 500

    keys = await _keys("u1")
    assert len(keys) <= 500
    # never_archive types untouched
    assert all(f"rule_{i}" in keys for i in range(5))
    assert all(f"instr_{i}" in keys for i in range(3))
    assert all(f"commit_{i}" in keys for i in range(2))
    # lowest activation evicted, high/mid survive
    assert not any(k.startswith("junk_") for k in keys)
    assert "high_0" in keys and "mid_0" in keys
    # eviction = archival, recoverable
    conn = await connection_manager.get("memory.db")
    archived = await (await conn.execute("SELECT COUNT(*) FROM archived_memories")).fetchone()
    assert archived[0] == 100
    still = await (await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id='u1'")).fetchone()
    assert still[0] == 500


@pytest.mark.asyncio
async def test_compact_noop_under_budget(cm):
    from lifecycle.compact import compact_under_budget

    now = time.time()
    await _insert("u2", "user", [(f"k{i}", "fact", 0.5, now) for i in range(10)])
    result = await compact_under_budget("u2", "user", budget=500)
    assert result == {"evicted": 0, "remaining": 10}


def test_retrieval_priority_per_kind():
    from shared.memory_types import MemoryKind, get_policy

    assert get_policy(MemoryKind.FACT).retrieval_priority == 0.9
    assert get_policy(MemoryKind.DECISION).retrieval_priority == 0.9
    assert get_policy(MemoryKind.COMMITMENT).retrieval_priority == 0.8
    assert get_policy(MemoryKind.CONTEXT).retrieval_priority == 0.1
    assert get_policy(MemoryKind.OBSERVATION).retrieval_priority == 0.3
    assert get_policy(MemoryKind.RULE).retrieval_priority == 0.9
    assert get_policy(MemoryKind.INSTRUCTION).retrieval_priority == 0.85
    assert get_policy(MemoryKind.PREFERENCE).retrieval_priority == 0.6
