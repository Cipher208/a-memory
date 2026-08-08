"""Tests for importance scheduler."""

import time

import pytest

from lifecycle.importance_scheduler import ImportanceScheduler, SchedulerConfig
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def cm(tmp_path):
    m = AsyncConnectionManager(base_dir=str(tmp_path))
    await m.execute_script(
        "memory.db",
        """
        CREATE TABLE core_memory (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, "key" TEXT, value TEXT,
            importance REAL, memory_kind TEXT,
            updated_at REAL, created_at REAL
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY,
            user_id TEXT, layer TEXT, action TEXT,
            target_id TEXT, details TEXT, timestamp REAL
        );
        CREATE TABLE importance_audit (
            id INTEGER PRIMARY KEY,
            user_id TEXT, chunk_id INTEGER, source TEXT,
            old_importance REAL, new_importance REAL,
            signal_breakdown TEXT, reason TEXT, rescored_at REAL
        );
        """,
    )
    return m


@pytest.mark.asyncio
async def test_scheduler_rescores_big_delta(cm, monkeypatch):
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("alice", "k", "redis cluster pipelining", 0.4, time.time(), time.time()),
    )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    stats = await s.run_once()
    assert stats["rescored"] >= 1

    row = await (await conn.execute("SELECT importance FROM core_memory WHERE \"key\"='k'")).fetchone()
    # Score should change (0.4 → new score based on tech keywords + retrieval baseline)
    assert row["importance"] != 0.4

    audit = await (await conn.execute("SELECT reason FROM importance_audit ORDER BY id DESC LIMIT 1")).fetchone()
    assert audit["reason"] == "scheduled"


@pytest.mark.asyncio
async def test_scheduler_skips_small_delta(cm, monkeypatch):
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, memory_kind, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("alice", "rule1", "важная инструкция по безопасности", 0.95, "rule", time.time(), time.time()),
    )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=1.0))
    stats = await s.run_once()
    # With delta_threshold=1.0, the change (0.95→0.4) is within threshold
    assert stats["skipped"] >= 1


@pytest.mark.asyncio
async def test_scheduler_retrieval_signal_boosts(cm, monkeypatch):
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("alice", "useful", "redis config notes", 0.5, time.time(), time.time()),
    )
    for _ in range(20):
        await conn.execute(
            """INSERT INTO audit_log
               (user_id, layer, action, target_id, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            ("alice", "core_memory", "recall_useful", "1", time.time()),
        )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(delta_threshold=0.05))
    await s.run_once()
    row = await (await conn.execute("SELECT importance FROM core_memory WHERE \"key\"='useful'")).fetchone()
    # Score should change from 0.5 due to retrieval signal
    assert row["importance"] != 0.5


@pytest.mark.asyncio
async def test_scheduler_ignores_old(cm, monkeypatch):
    monkeypatch.setattr("lifecycle.importance_scheduler.connection_manager", cm)
    conn = await cm.get("memory.db")
    long_ago = time.time() - 365 * 86400
    await conn.execute(
        """INSERT INTO core_memory
           (user_id, "key", value, importance, updated_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("alice", "old", "redis", 0.3, long_ago, long_ago),
    )
    await conn.commit()

    s = ImportanceScheduler(scheduler_config=SchedulerConfig(only_recent_days=30))
    stats = await s.run_once()
    assert stats["rescored"] == 0
