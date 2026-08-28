"""Tests for features.recall_telemetry (recall event store)."""

from __future__ import annotations

import time

import pytest

from features.recall_telemetry import ensure, record_recall, count_recalls
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME


@pytest.mark.asyncio
async def test_record_recall_inserts(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    await ensure(cm)
    eid = await record_recall(cm, "user", "default", "dark mode", "balanced", 3)
    assert eid > 0
    conn = await cm.get(DB_NAME)
    row = await (
        await conn.execute(
            "SELECT layer, user_id, query, intent, result_count FROM recall_events WHERE event_id=?",
            (eid,),
        )
    ).fetchone()
    assert row["layer"] == "user"
    assert row["user_id"] == "default"
    assert row["query"] == "dark mode"
    assert row["intent"] == "balanced"
    assert row["result_count"] == 3


@pytest.mark.asyncio
async def test_count_recalls_window(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    await ensure(cm)
    now = time.time()
    await record_recall(cm, "user", "default", "q1", "balanced", 1)
    await record_recall(cm, "user", "default", "q2", "balanced", 2)
    conn = await cm.get(DB_NAME)
    await conn.execute(
        "INSERT INTO recall_events (layer, user_id, query, intent, result_count, timestamp) VALUES ('user','default','old','balanced',0, ?)",
        (now - 1000,),
    )
    await conn.commit()
    n = await count_recalls(cm, "default", started_at=now - 100, ended_at=now + 100)
    assert n == 2


@pytest.mark.asyncio
async def test_count_recalls_all_no_window(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    await ensure(cm)
    await record_recall(cm, "user", "default", "q1", "balanced", 1)
    await record_recall(cm, "user", "default", "q2", "core", 0)
    n = await count_recalls(cm, "default")
    assert n == 2


@pytest.mark.asyncio
async def test_count_recalls_no_rows(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    await ensure(cm)
    assert await count_recalls(cm, "default") == 0


@pytest.mark.asyncio
async def test_record_recall_creates_table_lazily(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    eid = await record_recall(cm, "agent", "alice", "q", "recent", 0)
    assert eid > 0
    assert await count_recalls(cm, "alice") == 1
