"""D3.5: rehydrate block in build_inject_blocks."""

import sqlite3
from types import SimpleNamespace

import pytest

from shared.connection import connection_manager


class _FakeL4:
    async def get_all(self, user_id, limit):
        return [SimpleNamespace(key="k1", value="v1", importance=0.9)]


class _FakeL1:
    def get_recent(self, n):
        return []


class _FakeMem:
    l1 = _FakeL1()
    l4 = _FakeL4()


@pytest.fixture
def compaction_db(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS compaction_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " old_session_id TEXT, new_session_id TEXT, reason TEXT, summary TEXT,"
            " created_at REAL NOT NULL)"
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original


@pytest.mark.asyncio
async def test_fresh_compaction_yields_rehydrate_block(compaction_db):
    from features.inject import build_inject_blocks
    from features.rehydrate import log_compaction

    log_compaction("u1")
    blocks = await build_inject_blocks(_FakeMem(), None, "u1")
    kinds = [b["kind"] for b in blocks]
    assert "rehydrate" in kinds
    reh = next(b for b in blocks if b["kind"] == "rehydrate")
    assert "k1=v1" in reh["content"]


@pytest.mark.asyncio
async def test_no_compaction_no_rehydrate_block(compaction_db):
    from features.inject import build_inject_blocks

    blocks = await build_inject_blocks(_FakeMem(), None, "u1")
    assert all(b["kind"] != "rehydrate" for b in blocks)


@pytest.mark.asyncio
async def test_disabled_knob_suppresses_block(compaction_db, monkeypatch):
    from features.inject import build_inject_blocks
    from features.rehydrate import log_compaction

    log_compaction("u1")
    monkeypatch.setattr("features.rehydrate.rehydrate_enabled", lambda: False)
    blocks = await build_inject_blocks(_FakeMem(), None, "u1")
    assert all(b["kind"] != "rehydrate" for b in blocks)
