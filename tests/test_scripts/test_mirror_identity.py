"""S3 (2026-09-14): mirror_identity stable-key + replace semantics."""

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def db(tmp_path):
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path  # replace attr, never swap the object
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()
    connection_manager.base_dir = original


def test_section_key_stable_unique_and_readable() -> None:
    from scripts.mirror_identity import section_key

    k = section_key("rule", "AGENT", "Личность", 0)
    assert k.startswith("canon:rule:mir:AGENT:Личность#")
    assert k.endswith(":p0")
    assert k == section_key("rule", "AGENT", "Личность", 0)  # deterministic
    assert k != section_key("rule", "AGENT", "Личность и голод", 0)  # heading identity
    assert k != section_key("rule", "RULE", "Личность", 0)  # file identity
    assert k != section_key("rule", "AGENT", "Личность", 1)  # part identity


def test_split_chunks_returns_headed_parts() -> None:
    from scripts.mirror_identity import split_chunks

    md = (
        "## Alpha\n- first section body, long enough to survive the 30-char floor filter.\n\n"
        "## Beta\n- second section body, also comfortably above the minimum length filter.\n"
    )
    chunks = split_chunks(md)
    assert [(c[0], c[1]) for c in chunks] == [("Alpha", 0), ("Beta", 0)]
    assert all(len(c[2]) >= 30 for c in chunks)


_V1 = "## Личность\n- v1 body text that is clearly long enough for the chunk filter here.\n"
_V2 = "## Личность\n- v2 body text changed, but the section identity must stay the same.\n"
_DROP = "## Другая\n- a replacement section that no longer mentions the old one whatsoever.\n"


async def _rows(mem, user="u1"):
    return await mem.l4.get_all(user, 50)


@pytest.mark.asyncio
async def test_mirror_supersedes_on_edit_and_orphan_removes_deleted_section(db, tmp_path):
    from core import MemoryManager
    from scripts.mirror_identity import mirror

    mem = MemoryManager(cm=db).agent_memory("u1")
    f = tmp_path / "AGENT.md"

    f.write_text(_V1, encoding="utf-8")
    assert await mirror([f], "rule", "agent", "u1", 0.85, False, mem, db) == 1
    rows = await _rows(mem)
    assert len(rows) == 1 and "v1 body" in rows[0].value

    # body edit → same open row count, value replaced, temporal chain closed
    f.write_text(_V2, encoding="utf-8")
    await mirror([f], "rule", "agent", "u1", 0.85, False, mem, db)
    rows = await _rows(mem)
    assert len(rows) == 1 and "v2 body" in rows[0].value
    conn = await db.get("memory.db")
    closed = (await (await conn.execute("SELECT count(*) FROM core_memory_temporal WHERE valid_to IS NOT NULL")).fetchone())[0]
    assert closed >= 1

    # re-run unchanged → idempotent (still exactly one row)
    await mirror([f], "rule", "agent", "u1", 0.85, False, mem, db)
    assert len(await _rows(mem)) == 1

    # section removed → orphan pass deletes it, history keeps provenance
    f.write_text(_DROP, encoding="utf-8")
    await mirror([f], "rule", "agent", "u1", 0.85, False, mem, db)
    rows = await _rows(mem)
    assert len(rows) == 1 and "a replacement section" in rows[0].value
    hist = (await (await conn.execute("SELECT count(*) FROM core_memory_history WHERE key LIKE 'canon:rule:mir:AGENT:%'")).fetchone())[0]
    assert hist >= 1


@pytest.mark.asyncio
async def test_mirror_dry_run_writes_nothing(db, tmp_path):
    from core import MemoryManager
    from scripts.mirror_identity import mirror

    mem = MemoryManager(cm=db).agent_memory("u1")
    f = tmp_path / "AGENT.md"
    f.write_text(_V1, encoding="utf-8")
    assert await mirror([f], "rule", "agent", "u1", 0.85, True, mem, db) == 1
    assert await _rows(mem) == []
