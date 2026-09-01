"""D1.14 — snapshots + rollback on the A2.2 ledger."""

import json

import pytest

from core.memory import CoreMemory
from features import versioning as vs
from features.history import list_history
from shared.connection import AsyncConnectionManager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    mgr = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=mgr).migrate()
    return mgr


async def test_snapshot_create_list_and_duplicate_name(cm):
    core = CoreMemory(cm=cm, layer="user")
    await core.save("u1", "p_a", "original", importance=0.9)
    await core.save("u1", "p_b", "keep", importance=0.8)

    snap = await vs.snapshot_create(cm, "user", "u1", "saga1")
    assert snap["facts"] == 2 and snap["snapshot_id"] > 0

    listed = await vs.snapshot_list(cm, "u1")
    assert len(listed) == 1 and listed[0]["name"] == "saga1" and listed[0]["fact_count"] == 2
    assert "payload_json" not in listed[0]  # no payload bloat in listings

    with pytest.raises(ValueError):
        await vs.snapshot_create(cm, "user", "u1", "saga1")  # duplicate


async def test_snapshot_restore_exact_state(cm):
    core = CoreMemory(cm=cm, layer="user")
    await core.save("u1", "p_a", "original", importance=0.9)
    await vs.snapshot_create(cm, "user", "u1", "saga1")

    # diverge: change one, add one
    await core.save("u1", "p_a", "broken")
    await core.save("u1", "extra", "junk")

    out = await vs.snapshot_restore(cm, "user", "u1", "saga1")
    assert out["restored"] == 1 and out["deleted"] == 1

    assert (await core.get("u1", "p_a")).value == "original"
    assert (await core.get("u1", "p_a")).importance == 0.9
    assert await core.get("u1", "extra") is None

    # restore is ledger-traced
    hist = await list_history(cm, "u1", "user", key="extra")
    assert hist[0]["triggered_by"] == "snapshot_restore:saga1"

    # idempotent re-run: same counts, state unchanged
    out2 = await vs.snapshot_restore(cm, "user", "u1", "saga1")
    assert out2["restored"] == 1 and out2["deleted"] == 0


async def test_snapshot_restore_preserves_kind_source_metadata(cm):
    core = CoreMemory(cm=cm, layer="user")
    await core.save("u1", "decision:deploy", "ship friday", memory_kind="decision", source="staging_promotion", metadata={"typed": "decision"})
    await vs.snapshot_create(cm, "user", "u1", "pre_saga")
    await core.save("u1", "decision:deploy", "ship never")

    await vs.snapshot_restore(cm, "user", "u1", "pre_saga")
    conn = await cm.get("memory.db")
    row = await (
        await conn.execute("SELECT value, memory_kind, metadata FROM core_memory WHERE layer='user' AND user_id='u1' AND key='decision:deploy'")
    ).fetchone()
    assert row["value"] == "ship friday" and row["memory_kind"] == "decision"
    assert json.loads(row["metadata"])["typed"] == "decision"


async def test_rollback_insert_update_delete(cm):
    core = CoreMemory(cm=cm, layer="user")
    await core.save("u1", "k", "v1")  # insert
    await core.save("u1", "k", "v2", importance=0.9)  # update
    await core.delete("u1", "k")  # delete

    rows = await list_history(cm, "u1", "user")  # newest first: delete, update, insert

    # undo the DELETE → fact returns in its pre-delete state (v2 / 0.9)
    rb = await vs.rollback(cm, int(rows[0]["history_id"]))
    assert rb["action"] == "restored" and rb["key"] == "k"
    entry = await core.get("u1", "k")
    assert entry.value == "v2" and entry.importance == rows[0]["old_importance"]

    # undo the UPDATE → back to v1 — new ledger row tagged rollback:<N>
    rb2 = await vs.rollback(cm, int(rows[1]["history_id"]))
    assert rb2["action"] == "restored"
    assert (await core.get("u1", "k")).value == "v1"
    assert next(r["triggered_by"] for r in await list_history(cm, "u1", "user")) == f"rollback:{rows[1]['history_id']}"

    # undo the INSERT → fact deleted again
    rb3 = await vs.rollback(cm, int(rows[2]["history_id"]))
    assert rb3["action"] == "deleted"
    assert await core.get("u1", "k") is None

    with pytest.raises(ValueError):
        await vs.rollback(cm, 999999)


async def test_rollback_legacy_row_without_json(cm):
    conn = await cm.get("memory.db")
    # simulate a pre-d114 update row: no JSON, only value/importance
    await conn.execute(
        """INSERT INTO core_memory_history
           (layer, user_id, key, old_value, new_value, old_importance, new_importance, commit_hash, triggered_by, created_at)
           VALUES ('user', 'u1', 'legacy', 'old_v', 'new_v', 0.3, 0.7, 'deadbeefdeadbeef', 'manual', 123.0)"""
    )
    await conn.commit()
    core = CoreMemory(cm=cm, layer="user")
    await core.save("u1", "legacy", "new_v", importance=0.7)

    rb = await vs.rollback(cm, 1)
    assert rb["action"] == "restored"
    entry = await core.get("u1", "legacy")
    assert entry.value == "old_v" and entry.importance == 0.3
