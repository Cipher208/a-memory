"""memory_standing tool (A2.5 surface) — CRUD + run over .meta/*.yaml."""

import pytest

from shared.connection import connection_manager


@pytest.fixture()
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()

    async def migrate():
        from shared.migrations import MigrationManager

        await MigrationManager(cm=connection_manager).migrate()

    import asyncio

    asyncio.run(migrate())
    yield tmp_path
    connection_manager._conns.clear()


async def test_save_list_run_delete_roundtrip(hermetic):
    from mcp_server.tools.ops import memory_standing

    spec = '{"description": "открытые обязательства", "source": "core", "key_like": "commitment:%", "limit": 5}'
    res = await memory_standing("save", name="commitments", spec_json=spec)
    assert res["action"] == "save" and res["path"].endswith("commitments.yaml")

    lst = await memory_standing("list")
    assert [q["name"] for q in lst["queries"]] == ["commitments"]
    assert lst["queries"][0]["description"] == "открытые обязательства"

    run = await memory_standing("run", name="commitments")
    assert run["action"] == "run" and run["count"] == 0  # empty DB, DSL executed

    deleted = await memory_standing("delete", name="commitments")
    assert deleted["deleted"] is True
    lst2 = await memory_standing("list")
    assert lst2["queries"] == []


async def test_hostile_inputs_rejected(hermetic):
    from mcp_server.tools.ops import memory_standing

    with pytest.raises(ValueError, match="invalid standing query name"):
        await memory_standing("save", name="../../etc/passwd", spec_json="{}")
    with pytest.raises(ValueError, match="invalid standing query name"):
        await memory_standing("run", name="x" * 300)
    with pytest.raises(ValueError, match="not valid JSON"):
        await memory_standing("save", name="ok", spec_json="{unclosed")
    with pytest.raises(TypeError, match="JSON mapping"):
        await memory_standing("save", name="ok", spec_json='["a","b"]')
    with pytest.raises(ValueError, match="unknown filters"):
        await memory_standing("save", name="ok", spec_json='{"sql": "DROP TABLE users"}')
    with pytest.raises(ValueError, match="requires name"):
        await memory_standing("run")
    with pytest.raises(ValueError, match="unknown action"):
        await memory_standing("drop")


async def test_run_unknown_query_raises(hermetic):
    from mcp_server.tools.ops import memory_standing

    with pytest.raises(ValueError, match="unknown standing query"):
        await memory_standing("run", name="nonexistent")
