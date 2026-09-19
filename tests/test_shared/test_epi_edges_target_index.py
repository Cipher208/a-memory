"""19.09 (issue G): `epi_edges(target_id)` index must exist in a freshly migrated DB.

lateral_inhibition scans epi_edges with (source_id=? OR target_id=?) —
without this index the target_id half is a full-table scan (640k+ rows,
twice per inserted edge).
"""

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture()
def hermetic_cm(tmp_path, monkeypatch):
    """Migrate the global singleton onto tmp."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()

    async def migrate():
        await MigrationManager(cm=connection_manager).migrate()

    import asyncio

    asyncio.run(migrate())
    yield connection_manager
    connection_manager._conns.clear()


async def test_fresh_db_has_epi_edges_target_index(hermetic_cm):
    conn = await hermetic_cm.get("memory.db")
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_epi_edges_target'")
    assert await cur.fetchone() is not None
