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


def test_split_chunks_cuts_at_auto_drain_marker() -> None:
    """The hourly memory-drain cron writes transient facts below the
    `# === AUTO-DRAIN BELOW ===` marker and truncates them. That region must
    never reach the L4 mirror (canon) or move the drift-check hash (which would
    re-fire the session-start mirror-drift guard every hour)."""
    from scripts.mirror_identity import split_chunks

    md = (
        "## Stable\n- durable profile line, comfortably above the 30-char floor here.\n\n"
        "# === AUTO-DRAIN BELOW ===\n"
        "## Drained\n- transient drained fact that must not be mirrored, plenty long.\n"
    )
    text = "\n".join(c[2] for c in split_chunks(md))
    assert "durable profile line" in text
    assert "transient drained fact" not in text
    assert "AUTO-DRAIN" not in text


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


@pytest.mark.asyncio
async def test_migrate_sha_keys_deletes_mapped_keeps_unmapped(db, capsys):
    import time as _t

    from core import MemoryManager
    from scripts.mirror_identity import migrate_sha_keys

    mem = MemoryManager(cm=db).agent_memory("u1")
    conn = await db.get("memory.db")
    id1 = await mem.l4.save(
        "u1", "canon:rule:aaaaaaaaaaaa", "legacy mapped chunk body for the audit trail", 0.85, memory_kind="rule", source="identity_mirror"
    )
    await mem.l4.save(
        "u1", "canon:rule:bbbbbbbbbbbb", "legacy unmapped chunk body with no audit row", 0.85, memory_kind="rule", source="identity_mirror"
    )
    await conn.execute(
        "INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, signal_breakdown, reason, rescored_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("u1", int(id1), "identity_mirror", 0.0, 0.85, "{}", "mirror AGENT.md kind=rule", _t.time()),
    )
    await conn.commit()

    removed, kept = await migrate_sha_keys(mem, db, "u1", "agent")
    assert removed == 1
    assert kept == ["canon:rule:bbbbbbbbbbbb"]
    keys = {r.key for r in await _rows(mem)}
    assert "canon:rule:aaaaaaaaaaaa" not in keys and "canon:rule:bbbbbbbbbbbb" in keys
    hist = (await (await conn.execute("SELECT count(*) FROM core_memory_history WHERE key='canon:rule:aaaaaaaaaaaa'")).fetchone())[0]
    assert hist >= 1  # deletion is audited, not silent
    out = capsys.readouterr().out
    assert "unmapped" in out.lower()


@pytest.mark.asyncio
async def test_migrate_sha_keys_dry_run_touches_nothing(db, capsys):
    """--dry-run must preview, never delete — the flag guards live bases."""
    import time as _t

    from core import MemoryManager
    from scripts.mirror_identity import migrate_sha_keys

    mem = MemoryManager(cm=db).agent_memory("u1")
    conn = await db.get("memory.db")
    id1 = await mem.l4.save(
        "u1", "canon:rule:cccccccccccc", "legacy mapped chunk body for the audit trail", 0.85, memory_kind="rule", source="identity_mirror"
    )
    await conn.execute(
        "INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, signal_breakdown, reason, rescored_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("u1", int(id1), "identity_mirror", 0.0, 0.85, "{}", "mirror AGENT.md kind=rule", _t.time()),
    )
    await conn.commit()

    would, kept = await migrate_sha_keys(mem, db, "u1", "agent", dry=True)
    assert would == 1
    assert kept == []
    keys = {r.key for r in await _rows(mem)}
    assert "canon:rule:cccccccccccc" in keys  # nothing deleted in dry mode
    out = capsys.readouterr().out
    assert "dry" in out.lower()


_NEW = "## Третья\n- добавленный после зеркала раздел, которого в L4 ещё нет вовсе.\n"


@pytest.mark.asyncio
async def test_check_drift_reports_three_classes_without_writing(db, tmp_path):
    from core import MemoryManager
    from scripts.mirror_identity import check_drift, mirror

    mem = MemoryManager(cm=db).agent_memory("u1")
    f = tmp_path / "AGENT.md"

    f.write_text(_V1 + _DROP, encoding="utf-8")
    await mirror([f], "rule", "agent", "u1", 0.85, False, mem, db)
    assert len(await _rows(mem)) == 2

    # edit body (stale), delete a section (extra), add a section (missing) — all at once.
    f.write_text(_V2 + _NEW, encoding="utf-8")
    drift = await check_drift([f], "rule", "agent", "u1", db)
    assert len(drift["stale"]) == 1 and drift["stale"][0].startswith("canon:rule:mir:AGENT:Личность#")
    assert len(drift["missing"]) == 1 and drift["missing"][0].startswith("canon:rule:mir:AGENT:Третья#")
    assert len(drift["extra"]) == 1 and drift["extra"][0].startswith("canon:rule:mir:AGENT:Другая#")

    # check is read-only: L4 still holds the pre-edit rows
    rows = {r.key: r.value for r in await _rows(mem)}
    assert len(rows) == 2 and any("v1 body" in v for v in rows.values())

    # after re-mirror, the same check reports clean
    await mirror([f], "rule", "agent", "u1", 0.85, False, mem, db)
    assert await check_drift([f], "rule", "agent", "u1", db) == {"stale": [], "missing": [], "extra": []}


@pytest.mark.asyncio
async def test_namespace_overrides_disambiguate_same_stem_files(db, tmp_path):
    """Two different files sharing a stem must not orphan each other's rows."""

    from core import MemoryManager
    from scripts.mirror_identity import check_drift, mirror

    mem = MemoryManager(cm=db).agent_memory("u1")
    home = tmp_path / "AGENTS.md"
    cfg = tmp_path / "opencode" / "AGENTS.md"
    cfg.parent.mkdir()
    home.write_text("## Home only\n- раздел первого файла с тем же stem, никогда не见于 втором.\n", encoding="utf-8")
    cfg.write_text("## Cfg only\n- раздел второго файла с тем же stem, никогда не见于 первом.\n", encoding="utf-8")

    assert await mirror([home], "decision", "agent", "u1", 0.85, False, mem, db, ns="HOME") == 1
    assert await mirror([cfg], "decision", "agent", "u1", 0.85, False, mem, db, ns="CFG") == 1

    keys = {r.key for r in await _rows(mem)}
    assert len(keys) == 2  # neither run orphaned the other
    assert any(":mir:HOME:" in k for k in keys) and any(":mir:CFG:" in k for k in keys)
    assert await check_drift([cfg], "decision", "agent", "u1", db, ns="CFG") == {"stale": [], "missing": [], "extra": []}


@pytest.mark.asyncio
async def test_run_manifest_reports_drift_count_across_bases(db, tmp_path):
    import json

    from core import MemoryManager
    from scripts.mirror_identity import mirror, run_manifest

    mem = MemoryManager(cm=db).agent_memory("default")
    f = tmp_path / "PERSONA.md"
    f.write_text("## Frame\n- жёсткий фрейм личности, достаточно длинный для фильтра чанков.\n", encoding="utf-8")
    await mirror([f], "rule", "agent", "default", 0.85, False, mem, db)

    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps([{"base": str(tmp_path), "file": str(f), "kind": "rule", "layer": "agent"}]), encoding="utf-8")

    # in sync → zero drift
    assert await run_manifest(mf) == 0

    # file moves ahead of L4 → the edited section is counted as drift
    f.write_text("## Frame\n- правка после зеркала: L4 устарел, guard обязан это посчитать.\n", encoding="utf-8")
    assert await run_manifest(mf) == 1
