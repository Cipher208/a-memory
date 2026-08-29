"""D1.18: recall_useful signal + CLS replay nightly boost."""

import asyncio
import json
import time

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME

from features.replay import cls_replay, record_recall_useful


def _make_cm(tmp_path):
    from core.memory import CoreMemory
    from features.audit_trail import AuditTrail

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    core = CoreMemory(cm=cm, layer="user")

    async def init():
        await core._init_db()
        await AuditTrail(cm=cm)._init_db()
        conn = await cm.get(DB_NAME)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS importance_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT, chunk_id INTEGER, source TEXT,
                old_importance REAL, new_importance REAL,
                signal_breakdown TEXT, reason TEXT, rescored_at REAL)"""
        )
        await conn.commit()

    asyncio.run(init())
    return cm, core


def _fetchone(conn, sql, params=()):
    cur = asyncio.run(conn.execute(sql, params))
    return asyncio.run(cur.fetchone())


def _save(core, key, value, importance):
    asyncio.run(core.save("u1", key, value, importance=importance))


def test_record_and_replay_boost(tmp_path):
    """A recalled fact gets +0.05; unrecalled facts are untouched."""
    cm, core = _make_cm(tmp_path)
    _save(core, "hot", "recalled fact", importance=0.5)
    _save(core, "cold", "never recalled", importance=0.5)
    conn = asyncio.run(cm.get(DB_NAME))
    hot_id = int(_fetchone(conn, "SELECT entry_id FROM core_memory WHERE key='hot'")[0])

    n = asyncio.run(record_recall_useful(cm, "user", "u1", [(hot_id, "hot")]))
    assert n == 1

    result = asyncio.run(cls_replay(cm, "u1", layer="user"))
    assert result["boosted"] == 1

    row = _fetchone(conn, "SELECT importance FROM core_memory WHERE key='hot'")
    cold = _fetchone(conn, "SELECT importance FROM core_memory WHERE key='cold'")
    assert abs(float(row[0]) - 0.55) < 1e-9
    assert abs(float(cold[0]) - 0.5) < 1e-9

    # Audit row with reason=cls_replay
    audit = _fetchone(conn, "SELECT reason, new_importance FROM importance_audit WHERE reason='cls_replay'")
    assert audit is not None
    assert abs(float(audit[1]) - 0.55) < 1e-9


def test_replay_cap_and_stale_window(tmp_path):
    """Importance caps at 1.0; recall outside the window doesn't boost."""
    cm, core = _make_cm(tmp_path)
    _save(core, "maxed", "almost maxed", importance=0.99)
    _save(core, "old", "stale recall", importance=0.5)
    conn = asyncio.run(cm.get(DB_NAME))
    maxed_id = int(_fetchone(conn, "SELECT entry_id FROM core_memory WHERE key='maxed'")[0])
    old_id = int(_fetchone(conn, "SELECT entry_id FROM core_memory WHERE key='old'")[0])

    asyncio.run(record_recall_useful(cm, "user", "u1", [(maxed_id, "maxed"), (old_id, "old")]))
    # Backdate the 'old' recall beyond the 24h window
    asyncio.run(
        conn.execute("UPDATE audit_log SET timestamp=? WHERE target_id=?", (time.time() - 48 * 3600, str(old_id)))
    )
    asyncio.run(conn.commit())

    result = asyncio.run(cls_replay(cm, "u1", layer="user", window_hours=24))
    assert result["boosted"] == 1  # only 'maxed' inside window

    maxed = _fetchone(conn, "SELECT importance FROM core_memory WHERE key='maxed'")
    old = _fetchone(conn, "SELECT importance FROM core_memory WHERE key='old'")
    assert abs(float(maxed[0]) - 1.0) < 1e-9
    assert abs(float(old[0]) - 0.5) < 1e-9
    assert json.dumps(result)  # counters serializable
