"""A2.2 — core_memory_history ledger: every L4 mutation leaves a scar."""

import hashlib

import pytest

from core.memory import CoreMemory
from shared.connection import AsyncConnectionManager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    mgr = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=mgr).migrate()
    return mgr


async def _rows(cm, user_id="u1", layer="user", key=""):
    conn = await cm.get("memory.db")
    if key:
        cur = await conn.execute(
            "SELECT * FROM core_memory_history WHERE user_id=? AND layer=? AND key=? ORDER BY history_id DESC",
            (user_id, layer, key),
        )
    else:
        cur = await conn.execute(
            "SELECT * FROM core_memory_history WHERE user_id=? AND layer=? ORDER BY history_id DESC", (user_id, layer)
        )
    return [dict(r) for r in await cur.fetchall()]


async def test_insert_update_delete_all_logged(cm):
    core = CoreMemory(cm=cm, layer="user")
    await core.save("u1", "k1", "v1")  # insert
    await core.save("u1", "k1", "v2", importance=0.8)  # update
    assert await core.delete("u1", "k1")  # delete

    rows = await _rows(cm)
    assert len(rows) == 3
    assert rows[0]["new_value"] is None and rows[0]["old_value"] == "v2"  # delete
    assert rows[1]["old_value"] == "v1" and rows[1]["new_value"] == "v2"  # update
    assert rows[2]["old_value"] is None and rows[2]["new_value"] == "v1"  # insert
    assert rows[1]["new_importance"] == 0.8


async def test_commit_hash_deterministic(cm):
    core = CoreMemory(cm=cm)
    await core.save("u1", "k", "v")
    rows = await _rows(cm)
    expected = hashlib.sha256("user|u1|k|None|v".encode()).hexdigest()[:16]
    assert rows[0]["commit_hash"] == expected


async def test_triggered_by_rides_source_and_overrides(cm):
    core = CoreMemory(cm=cm)
    await core.save("u1", "k", "v", source="staging_promotion")
    await core.save("u1", "k", "v2", source="drain", triggered_by="branch_merge:exp1")
    await core.delete("u1", "k")
    rows = await _rows(cm)
    assert [r["triggered_by"] for r in rows] == ["delete", "branch_merge:exp1", "staging_promotion"]


async def test_history_failure_never_blocks_save(cm):
    class BoomConn:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("ledger exploded")

    core = CoreMemory(cm=cm)
    eid = await core.save("u1", "k", "v")  # normal path: save + ledger row
    assert eid > 0

    # the guard itself: a failing ledger write must never raise
    await core._record_history(BoomConn(), "user", "u1", "k", None, ("v", 0.5), "t", 0.0)


async def test_list_history_filters_and_get_row(cm):
    from features.history import get_history_row, list_history

    core = CoreMemory(cm=cm, layer="user")
    await core.save("u1", "ka", "va")
    await core.save("u1", "kb", "vb")
    await core.save("u2", "ka", "other-user")  # other user — filtered out

    assert len(await list_history(cm, "u1", "user")) == 2
    ka = await list_history(cm, "u1", "user", key="ka")
    assert len(ka) == 1 and ka[0]["key"] == "ka"

    row = await get_history_row(cm, int(ka[0]["history_id"]))
    assert row is not None and row["new_value"] == "va"
    assert await get_history_row(cm, 999999) is None
