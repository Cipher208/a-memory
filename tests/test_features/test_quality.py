"""D1.19: memory quality metrics — was_useful → score (agent feedback loop)."""

import sqlite3
import time

import pytest

from shared.connection import connection_manager


@pytest.fixture
def quality_db(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS core_memory ("
            " entry_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', key TEXT NOT NULL, value TEXT NOT NULL,"
            " importance REAL DEFAULT 0.5, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, action TEXT NOT NULL,"
            " layer TEXT NOT NULL, target_id TEXT, details TEXT, timestamp REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS importance_audit ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, chunk_id INTEGER,"
            " source TEXT NOT NULL, old_importance REAL, new_importance REAL,"
            " signal_breakdown TEXT, reason TEXT, rescored_at REAL NOT NULL)"
        )
        now = time.time()
        conn.execute(
            "INSERT INTO core_memory (user_id, key, value, importance, created_at, updated_at) VALUES ('u1', 'deploy', 'ssh then sync', 0.5, ?, ?)",
            (now, now),
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original


def _entry_id(db) -> int:
    with sqlite3.connect(str(db)) as conn:
        return int(conn.execute("SELECT entry_id FROM core_memory").fetchone()[0])


@pytest.mark.asyncio
async def test_feedback_useful_boosts_and_logs(quality_db):
    from features.quality import record_feedback

    eid = _entry_id(quality_db)
    res = await record_feedback("u1", "user", eid, useful=True)
    assert res["old"] == 0.5 and res["new"] > 0.5
    with sqlite3.connect(str(quality_db)) as conn:
        conn.row_factory = sqlite3.Row
        acts = [r["action"] for r in conn.execute("SELECT action FROM audit_log").fetchall()]
        assert acts.count("recall_useful") == 1  # feeds ACT-R frequency
        audit = conn.execute("SELECT reason, new_importance FROM importance_audit").fetchone()
        assert audit["reason"] == "agent_feedback"


@pytest.mark.asyncio
async def test_feedback_not_useful_decays_with_floor(quality_db):
    from features.quality import record_feedback

    eid = _entry_id(quality_db)
    for _ in range(12):  # hammer decay — floor must hold
        await record_feedback("u1", "user", eid, useful=False)
    with sqlite3.connect(str(quality_db)) as conn:
        imp = float(conn.execute("SELECT importance FROM core_memory").fetchone()[0])
    assert imp == 0.05  # floor
    assert imp >= 0.05


@pytest.mark.asyncio
async def test_report_aggregates(quality_db):
    from features.quality import record_feedback, quality_report

    eid = _entry_id(quality_db)
    await record_feedback("u1", "user", eid, useful=True)
    await record_feedback("u1", "user", eid, useful=True)
    rep = await quality_report("u1", "user")
    assert rep["total_tracked"] >= 1
    top = rep["top_useful"][0]
    assert top["entry_id"] == eid and top["useful_count"] == 2
    assert top["key"] == "deploy"
