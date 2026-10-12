"""Reclassify must not overrule a declared kind, and must show its plan first.

Two defects, both measured on the live base before they were fixed:

- the curated rows the identity mirror and `think(kind=...)` write were re-derived
  from their text like any other row, so a bulk reclassify would have demoted seven
  authored rows (six `canon:rule:mir:SOUL:*` and one declared `canon:rule:<sha>`);
- `--dry-run` built its change list and returned it in silence, so the destructive
  path had no preview at all.

The rows below are seeded through the real table shape rather than through a writer,
because the subject here is the classifier's scope, not the write path.
"""

from __future__ import annotations

import sys

import pytest

from shared.connection import connection_manager


@pytest.fixture
async def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    yield tmp_path
    connection_manager._conns.clear()


async def _seed(user_id: str, key: str, value: str, kind: str, source: str) -> int:
    conn = await connection_manager.get("memory.db")
    await conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, memory_kind,"
        " source, created_at, updated_at) VALUES ('user', ?, ?, ?, 0.5, ?, ?, 1, 1)",
        (user_id, key, value, kind, source),
    )
    await conn.commit()
    return int((await (await conn.execute("SELECT last_insert_rowid() as i")).fetchone())["i"])


async def _kind_of(entry_id: int) -> str:
    conn = await connection_manager.get("memory.db")
    row = await (await conn.execute("SELECT memory_kind FROM core_memory WHERE entry_id=?", (entry_id,))).fetchone()
    return str(row["memory_kind"])


# Both curated sources the house already names in `ImportanceScheduler.CURATED_SOURCES`
# and that this table actually carries: the mirror's `--kind`, and a declared canon.
CURATED_SEEDS = [
    ("identity_mirror", "canon:rule:mir:SOUL:1_Identity"),
    ("user_explicit", "canon:rule:74f3bce1f474"),
]


@pytest.mark.parametrize(("source", "key"), CURATED_SEEDS)
async def test_declared_kind_survives_apply(hermetic_base, source, key):
    """A curated row keeps the kind it declared, even when its text reads otherwise."""
    eid = await _seed("curator", key, "I committed to ship the fix", "rule", source)

    changes = await _reclassify("curator", dry_run=False)

    assert await _kind_of(eid) == "rule", f"{source} row was overruled by the text heuristic"
    assert all(rid != eid for _old, _new, rid in changes), "a curated row must not appear in the plan"


async def test_preview_prints_plan_and_writes_nothing(hermetic_base, capsys):
    """The default path is a preview: the plan is visible and the store is untouched."""
    eid = await _seed("organic", "o:ship", "I decided to ship it", "fact", "new_message")

    changes = await _reclassify("organic")

    out = capsys.readouterr().out
    assert "dry run" in out and "would change" in out, f"preview must state its plan and its mode: {out!r}"
    assert await _kind_of(eid) == "fact", "preview must not write"
    assert changes, "the plan is returned so a caller need not parse stdout"


async def test_organic_row_is_reclassified_on_apply(hermetic_base):
    """The write still happens — for a row whose kind the text genuinely decides."""
    eid = await _seed("organic", "o:ship", "I committed to ship the fix", "fact", "new_message")

    await _reclassify("organic", dry_run=False)

    assert await _kind_of(eid) == "commitment", "an organic row follows its text"


def test_cli_apply_is_the_switch(hermetic_base, monkeypatch, capsys):
    """`--apply` writes; its absence previews. The CLI wiring, not just the function."""
    import asyncio

    eid = asyncio.run(_seed("cliuser", "c:ship", "I committed to ship the fix", "fact", "new_message"))

    from features.typed_export import main

    monkeypatch.setattr(sys, "argv", ["typed_export", "reclassify", "--user", "cliuser"])
    main()
    assert "dry run" in capsys.readouterr().out
    assert asyncio.run(_kind_of(eid)) == "fact", "no --apply, no write"

    monkeypatch.setattr(sys, "argv", ["typed_export", "reclassify", "--user", "cliuser", "--apply"])
    main()
    assert asyncio.run(_kind_of(eid)) == "commitment", "--apply performs the plan"


def test_cli_process_actually_exits(hermetic_base):
    """Regression: the module did its work and then hung until killed (`exit=124`).

    aiosqlite's worker threads are non-daemon, so a CLI that leaves a connection open
    never returns — only `ariel_cli` closed them, and this module is also run directly.
    A subprocess is the only way to see it: the defect lives at interpreter shutdown.
    """
    import os
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "MCP_MEMORY_DATA_DIR": str(hermetic_base), "PYTHONPATH": ""}
    proc = subprocess.run(
        [sys.executable, "-u", "-m", "features.typed_export", "reclassify", "--user", "nobody"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"process hung or failed: {proc.stderr[-600:]}"
    assert "reclassify:" in proc.stdout, proc.stdout


async def _reclassify(user_id: str, dry_run: bool = True):
    from features.typed_export import do_reclassify

    return await do_reclassify(user_id, dry_run=dry_run)
