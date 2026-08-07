import time

import pytest

from lifecycle.compactor import MemoryCompactor
from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.metrics import metrics


@pytest.mark.asyncio
async def test_metrics_registry():
    """Verify that our custom metrics are in the Prometheus registry."""
    # Trigger some metrics
    metrics.memory_ops_total.labels(action="test_op", layer="test_layer").inc()
    metrics.current_importance_threshold.set(0.42)

    # Render and check
    output = metrics.render_prometheus()
    assert "ariel_memory_ops_total" in output
    assert 'action="test_op"' in output
    assert "ariel_memory_importance_threshold 0.42" in output

@pytest.mark.asyncio
async def test_compactor_logic(tmp_path, monkeypatch):
    """Verify memory compaction (archiving old low-importance items)."""
    # 1. Setup mock DB data
    conn = await connection_manager.get(DB_NAME)
    user_id = "test_user_compactor"

    # Clean old data if any
    await conn.execute("DELETE FROM core_memory WHERE user_id=?", (user_id,))
    await conn.execute("DELETE FROM archived_memories WHERE user_id=?", (user_id,))

    now = time.time()
    # Insert a fresh important memory (should stay)
    await conn.execute(
        "INSERT INTO core_memory (user_id, key, value, importance, memory_kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, "stay", "important fresh", 0.9, "fact", now, now)
    )

    # Insert an old unimportant memory (should be archived)
    old_time = now - (10 * 86400) # 10 days ago
    await conn.execute(
        "INSERT INTO core_memory (user_id, key, value, importance, memory_kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, "go", "old boring", 0.2, "chitchat", old_time, old_time)
    )
    await conn.commit()

    # 2. Run compactor
    compactor = MemoryCompactor(age_days=7, min_importance=0.4)
    result = await compactor.run_cleanup(user_id=user_id)

    assert result["archived"] == 1

    # 3. Verify core_memory state
    cur = await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    assert row[0] == 1

    # 4. Verify archived_memories state
    cur = await conn.execute("SELECT content FROM archived_memories WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    assert row[0] == "old boring"
