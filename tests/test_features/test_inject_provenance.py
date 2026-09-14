"""S4 (2026-09-14): L4 rows entering context carry their UTC date provenance."""

import sqlite3
from types import SimpleNamespace

import pytest

from shared.connection import connection_manager

TS = 1788880035.0  # 2026-09-08T15:07Z


class _FakeL4:
    def __init__(self, rows):
        self._rows = rows

    async def get_all(self, user_id, limit):
        return self._rows

    async def get_pinned(self, user_id, limit):
        return []


class _FakeL3:
    async def search_by_tag(self, user_id, tag, limit=10):
        return []


class _FakeL1:
    def get_recent(self, n):
        return []


class _FakeMem:
    def __init__(self, rows):
        self.l1 = _FakeL1()
        self.l3 = _FakeL3()
        self.l4 = _FakeL4(rows)


def _row(key, value, importance=0.9, updated_at=TS, created_at=TS - 86400, kind="rule"):
    ns = SimpleNamespace(key=key, value=value, importance=importance, memory_kind=kind)
    if updated_at is not None:
        ns.updated_at = updated_at
    if created_at is not None:
        ns.created_at = created_at
    return ns


@pytest.fixture
def compaction_free(tmp_path, monkeypatch):
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
    yield tmp_path
    connection_manager.base_dir = original


@pytest.mark.asyncio
async def test_important_block_stamps_date(compaction_free):
    from features.inject import build_inject_blocks

    blocks = await build_inject_blocks(_FakeMem([_row("canon:rule:demo", "always lint")]), None, "u1", budget=500)
    imp = [b for b in blocks if b["kind"] == "important"]
    assert imp and "2026-09-08 canon:rule:demo=always lint" in imp[0]["content"]


@pytest.mark.asyncio
async def test_important_block_undated_rows_unchanged(compaction_free):
    from features.inject import build_inject_blocks

    rows = [SimpleNamespace(key="k1", value="v1", importance=0.9, memory_kind="fact")]
    blocks = await build_inject_blocks(_FakeMem(rows), None, "u1", budget=500)
    imp = [b for b in blocks if b["kind"] == "important"]
    assert imp and "k1=v1" in imp[0]["content"]
    assert "2026-09" not in imp[0]["content"]


@pytest.mark.asyncio
async def test_rehydrate_block_stamps_date(compaction_free):
    from features.inject import build_inject_blocks
    from features.rehydrate import log_compaction

    log_compaction("u1")
    blocks = await build_inject_blocks(_FakeMem([_row("dream_x", "compact survival note")]), None, "u1", budget=900)
    reh = [b for b in blocks if b["kind"] == "rehydrate"]
    assert reh and "2026-09-08 dream_x=compact survival note" in reh[0]["content"]
