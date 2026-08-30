"""D1.6 memory_fact_blame — provenance + evidence trail over existing substrate."""

import sqlite3
import time

import pytest

from shared.connection import connection_manager


@pytest.fixture
async def blame_db(tmp_path, monkeypatch):
    """Isolate base_dir; close_all() on both sides — connection_manager caches
    aiosqlite conns by DB_NAME key, so a stale tmp-dir connection leaks in."""
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    await connection_manager.close_all()
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS core_memory ("
            " entry_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', key TEXT NOT NULL, value TEXT NOT NULL,"
            " importance REAL DEFAULT 0.5, memory_kind TEXT, expires_at REAL,"
            " source TEXT DEFAULT 'manual', metadata TEXT,"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL)"
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
            "INSERT INTO core_memory (user_id, key, value, importance, source, created_at, updated_at)"
            " VALUES ('u1', 'deploy', 'ssh then sync', 0.6, 'user_explicit', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO core_memory (user_id, key, value, importance, source, created_at, updated_at)"
            " VALUES ('u1', 'summary', 'merged episode fact', 0.5, 'staging_promotion', ?, ?)",
            (now, now),
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original
    await connection_manager.close_all()


def _entry_id(db, key: str) -> int:
    with sqlite3.connect(str(db)) as conn:
        return int(conn.execute("SELECT entry_id FROM core_memory WHERE key=?", (key,)).fetchone()[0])


@pytest.mark.asyncio
async def test_blame_by_entry_id(blame_db):
    from features.blame import fact_blame

    eid = _entry_id(blame_db, "deploy")
    res = await fact_blame("u1", "user", entry_id=eid)
    assert res["provenance"] == "user_explicit"
    assert res["key"] == "deploy" and res["value"] == "ssh then sync"
    assert res["counts"] == {"importance_changes": 0, "audit_events": 0}


@pytest.mark.asyncio
async def test_blame_by_key(blame_db):
    from features.blame import fact_blame

    res = await fact_blame("u1", "user", key="summary")
    assert res["provenance"] == "staging_promotion"


@pytest.mark.asyncio
async def test_blame_evidence_trail(blame_db):
    from features.blame import fact_blame

    eid = _entry_id(blame_db, "deploy")
    now = time.time()
    with sqlite3.connect(str(blame_db)) as conn:
        conn.execute(
            "INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, reason, rescored_at)"
            " VALUES ('u1', ?, 'cls_replay', 0.55, 0.6, 'cls_replay', ?)",
            (eid, now),
        )
        conn.execute(
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES ('u1', 'recall_useful', 'user', ?, '{}', ?)",
            (str(eid), now),
        )
        conn.commit()
    res = await fact_blame("u1", "user", entry_id=eid)
    assert res["counts"]["importance_changes"] == 1
    assert res["importance_history"][0]["reason"] == "cls_replay"
    assert res["counts"]["audit_events"] == 1
    assert res["audit_events"][0]["action"] == "recall_useful"


@pytest.mark.asyncio
async def test_blame_missing_entry_raises(blame_db):
    from features.blame import fact_blame

    with pytest.raises(ValueError):
        await fact_blame("u1", "user", entry_id=999)
    with pytest.raises(ValueError):
        await fact_blame("u1", "user")


@pytest.mark.asyncio
async def test_remember_defaults_to_user_explicit(blame_db):
    from core import MemoryLayer
    from features.blame import fact_blame

    mem = MemoryLayer("user", user_id="u1")
    await mem.l4._init_db()
    eid = await mem.remember("new_fact", "value here", 0.7)
    res = await fact_blame("u1", "user", entry_id=eid)
    assert res["provenance"] == "user_explicit"
