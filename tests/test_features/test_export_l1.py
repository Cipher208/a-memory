"""E4: export v1.2 includes L1 ring content; typed_export writes a file."""

import json

import pytest

from shared.connection import connection_manager


@pytest.fixture
async def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    return tmp_path


async def test_export_includes_l1(hermetic_base):
    from core import memory_manager
    from features.import_export import ImportExport

    ie = ImportExport(cm=connection_manager)
    buf = memory_manager.user_memory("exportee").l1
    buf.clear()
    buf.add("user", "ring entry one", tokens=1)
    buf.add("assistant", "reply one", tokens=2)

    path = await ie.export_user("exportee")
    payload = json.loads((hermetic_base / "exports" / path.split("/")[-1]).read_text())
    assert payload["version"] == "1.2"
    assert [e["content"] for e in payload["l1"]["user"]] == ["ring entry one", "reply one"]
    assert payload["l1"]["agent"] == []


async def test_import_restores_l1(hermetic_base):
    from core import memory_manager
    from features.import_export import ImportExport

    ie = ImportExport(cm=connection_manager)
    buf = memory_manager.user_memory("importee").l1
    buf.clear()
    buf.add("user", "precious ring entry", tokens=1)
    path = await ie.export_user("importee")

    buf.clear()
    assert buf.size() == 0
    res = await ie.import_user(path.split("/")[-1], target_user_id="importee")
    assert res["l1"] == 1
    assert memory_manager.user_memory("importee").l1.get_full()[-1].content == "precious ring entry"


def test_v11_payload_imports_without_l1(hermetic_base):
    """Backward compat: v1.1 export files (no l1 key) still import."""

    from features.import_export import ImportExport

    ie = ImportExport(cm=connection_manager)
    f = ie.export_dir / "old_v11.json"
    f.write_text(
        json.dumps({"user_id": "u1", "version": "1.1", "core_memory": [], "episodes": [], "sessions": []})
    )

    res = _run(ie.import_user("old_v11.json", target_user_id="u2"))
    assert res.get("l1", 0) == 0


def _run(coro):
    import asyncio

    return asyncio.run(coro)


async def test_agent_layer_l1_exports_and_imports(hermetic_base):
    from core import memory_manager
    from features.import_export import ImportExport

    ie = ImportExport(cm=connection_manager)
    abuf = memory_manager.agent_memory("layered").l1
    abuf.clear()
    abuf.add("agent", "agent ring entry", tokens=1)
    path = await ie.export_user("layered")
    payload = json.loads((hermetic_base / "exports" / path.split("/")[-1]).read_text())
    assert payload["l1"]["agent"][-1]["content"] == "agent ring entry"

    abuf.clear()
    await ie.import_user(path.split("/")[-1], target_user_id="layered")
    assert memory_manager.agent_memory("layered").l1.get_full()[-1].content == "agent ring entry"
