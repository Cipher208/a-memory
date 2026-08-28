"""Tests for import_export path traversal prevention."""

import pytest

from features.import_export import ImportExport


@pytest.fixture
def ie(tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()

    class FakeCM:
        def __init__(self, base):
            self._base = base

        @property
        def base_dir(self):
            return self._base

    obj = ImportExport.__new__(ImportExport)
    obj._cm = FakeCM(tmp_path)
    obj.export_dir = export_dir
    return obj


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd"])
def test_import_rejects_traversal(ie, path):
    import asyncio

    with pytest.raises(ValueError, match="escapes base directory"):
        asyncio.run(ie.import_user(path))


def test_import_user_bulk_insert(tmp_path):
    """A2.8: import_user inserts core_memory/episodes/sessions in bulk."""
    import asyncio
    import json

    from core.episodic import EpisodicMemory
    from core.memory import CoreMemory
    from core.session import SessionStore
    from shared.connection import AsyncConnectionManager

    from features.import_export import ImportExport

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        # Production startup runs these _init_db calls; mirror it here.
        await CoreMemory(cm=cm)._init_db()
        await EpisodicMemory(cm=cm)._init_db()
        await SessionStore(cm=cm)._init_db()

        ie = ImportExport(cm=cm)
        export_file = ie.export_dir / "export.json"
        export_file.write_text(
            json.dumps(
                {
                    "user_id": "u1",
                    "core_memory": [
                        {"layer": "user", "key": "k1", "value": "v1", "importance": 0.9,
                         "memory_kind": "fact", "created_at": 100.0, "updated_at": 100.0},
                        {"layer": "user", "key": "k2", "value": "v2", "importance": 0.5,
                         "memory_kind": "fact", "created_at": 101.0, "updated_at": 101.0},
                    ],
                    "episodes": [
                        {"layer": "user", "summary": "s1", "emotional_weight": 0.7,
                         "tags": "[]", "created_at": 102.0},
                    ],
                    "sessions": [],
                }
            ),
            encoding="utf-8",
        )
        imported = await ie.import_user(str(export_file))
        assert imported == {"core_memory": 2, "episodes": 1, "sessions": 0}

        conn = await cm.get("memory.db")
        cur = await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id='u1'")
        assert (await cur.fetchone())[0] == 2
        cur = await conn.execute("SELECT COUNT(*) FROM episodes WHERE user_id='u1'")
        assert (await cur.fetchone())[0] == 1

    asyncio.run(t())
