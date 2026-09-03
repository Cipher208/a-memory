"""A2.5: standing queries — .meta/* yaml files evaluated through the D1.7 DSL."""

import asyncio

import pytest

from features import standing_queries as sq
from shared.connection import connection_manager


@pytest.fixture()
def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


async def test_save_run_delete_roundtrip(hermetic_base):
    import sqlite3

    conn = sqlite3.connect(hermetic_base / "memory.db")
    conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, created_at, updated_at)"
        " VALUES ('user', 'default', 'commitment:ship', 'ship v2', 0.8, 1, 1)"
    )
    conn.commit()
    conn.close()

    path = sq.save_standing("commitments", {"description": "open commitments", "source": "core", "key_like": "commitment:%"})
    assert path.is_file()

    listed = sq.list_standing()
    assert [li["name"] for li in listed] == ["commitments"]

    res = await sq.run_standing("commitments", "default")
    assert res["count"] == 1 and res["rows"][0]["key"] == "commitment:ship"

    assert sq.delete_standing("commitments") is True
    assert sq.list_standing() == []


async def test_validation_unknown_filter(hermetic_base):
    with pytest.raises(ValueError, match="unknown filters"):
        sq.save_standing("bad", {"sql": "DROP TABLE x"})
    with pytest.raises(ValueError, match="unknown standing query"):
        await sq.run_standing("nonexistent")


async def test_name_injection_rejected(hermetic_base):
    with pytest.raises(ValueError, match="invalid standing query name"):
        sq.save_standing("../../etc/passwd", {"source": "core"})
    with pytest.raises(ValueError, match="invalid standing query name"):
        await sq.run_standing("../evil")
