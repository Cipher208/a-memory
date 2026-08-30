"""D1.15: agent scratchpad (L2.5 working memory) — table helpers + inject block."""

import sqlite3

import pytest

from shared.connection import connection_manager


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_scratchpad ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', key TEXT NOT NULL,"
            " content TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
            " UNIQUE(user_id, layer, key))"
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original


@pytest.mark.asyncio
async def test_write_upsert_and_cap_evict(scratch_db):
    from features.scratchpad import write_entry, read_entries

    await write_entry("u1", "user", "hypothesis", "cache is the bottleneck")
    await write_entry("u1", "user", "hypothesis", "cache is the bottleneck v2")  # upsert
    for i in range(25):
        await write_entry("u1", "user", f"note{i}", f"content {i}")
    entries = read_entries("u1", "user")
    assert len(entries) <= 20  # cap evicts oldest (hypothesis aged out)
    keys = [e["key"] for e in entries]
    assert "note24" in keys
    await write_entry("u1", "user", "hypothesis", "refreshed")  # re-upsert
    keys = [e["key"] for e in read_entries("u1", "user")]
    assert "hypothesis" in keys
    one = read_entries("u1", "user", key="hypothesis")
    assert one[0]["content"] == "refreshed"


@pytest.mark.asyncio
async def test_clear_one_and_all(scratch_db):
    from features.scratchpad import write_entry, clear_entries, read_entries

    await write_entry("u1", "user", "a", "1")
    await write_entry("u1", "user", "b", "2")
    clear_entries("u1", "user", key="a")
    assert [e["key"] for e in read_entries("u1", "user")] == ["b"]
    clear_entries("u1", "user")
    assert read_entries("u1", "user") == []


@pytest.mark.asyncio
async def test_promote_to_l3_and_clear(scratch_db):
    from features.scratchpad import write_entry, promote_entries, read_entries

    await write_entry("u1", "user", "plan", "migrate to tencent first")
    saved = []

    class _FakeMem:
        class l3:  # noqa: N801
            @staticmethod
            async def save(user_id, summary, weight, tags):
                saved.append((user_id, summary, weight, tags))
                return 1

    res = await promote_entries(_FakeMem(), "u1", "user", keys=["plan"], to="l3")
    assert res["count"] == 1
    assert saved[0][1] == "migrate to tencent first"
    assert "scratchpad_promoted" in saved[0][3]
    assert read_entries("u1", "user") == []  # promoted entries leave the pad


@pytest.mark.asyncio
async def test_inject_scratchpad_block(scratch_db):
    from features.scratchpad import write_entry
    from features.inject import build_inject_blocks

    await write_entry("u1", "user", "hypothesis", "check the WAL size first")

    class _FakeMem:
        class l1:  # noqa: N801
            @staticmethod
            def get_recent(n):
                return []

        class l4:  # noqa: N801
            @staticmethod
            async def get_all(user_id, limit):
                return []

    blocks = await build_inject_blocks(_FakeMem(), None, "u1")
    kinds = [b["kind"] for b in blocks]
    assert "scratchpad" in kinds
    block = next(b for b in blocks if b["kind"] == "scratchpad")
    assert "hypothesis" in block["content"]
