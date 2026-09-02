"""E4: typed_export.do_export must write a file (was: fetch + return, no write)."""

import json
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager


@pytest.fixture
async def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    return tmp_path


async def test_do_export_writes_file(hermetic_base):
    from features.typed_export import do_export
    from shared.migrations import migration_manager

    await migration_manager.migrate()

    # seed one typed row through the real write path
    conn = await connection_manager.get("memory.db")
    await conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, memory_kind, created_at, updated_at)"
        " VALUES ('user', 'typewriter', 't:note', 'hello typed world', 0.5, 'instruction', 1, 1)"
    )
    await conn.commit()

    out = await do_export("typewriter", "instruction")
    assert out is not None and out.exists(), "do_export must write a file"
    payload = json.loads(out.read_text())
    assert payload["memory_kind"] == "instruction"
    assert [r["key"] for r in payload["rows"]] == ["t:note"]


async def test_do_export_all_kinds(hermetic_base):
    from features.typed_export import do_export
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    out = await do_export("emptyuser", None)
    assert out is not None and out.exists()
    payload = json.loads(out.read_text())
    assert payload["rows"] == []
