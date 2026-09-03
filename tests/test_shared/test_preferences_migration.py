"""A2.5-a25: `preferences` must exist in a freshly migrated DB.

The table was a pre-v8 legacy survivor: prod DBs had it, the migration chain
didn't — AdaptiveThresholdManager broke on first write to a fresh deploy
(audit finding #3).
"""

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture()
def hermetic_cm(tmp_path, monkeypatch):
    """Migrate the global singleton onto tmp (AdaptiveThresholdManager uses
    the module-level import — patching .base_dir is the only consistent way)."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()

    async def migrate():
        await MigrationManager(cm=connection_manager).migrate()

    import asyncio

    asyncio.run(migrate())
    yield connection_manager
    connection_manager._conns.clear()


async def test_fresh_db_has_preferences(hermetic_cm):
    conn = await hermetic_cm.get("memory.db")
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='preferences'")
    assert await cur.fetchone() is not None


async def test_adaptive_threshold_round_trip_fresh_db(hermetic_cm):
    """The actual consumer works on a fresh deploy: load → default, update → persists."""
    from shared.adaptive import AdaptiveThresholdManager

    mgr = AdaptiveThresholdManager(key="test_threshold_ema")
    mgr._current_value = None  # not the shared singleton — clean cache
    assert await mgr.get_threshold() == AdaptiveThresholdManager.DEFAULT_THRESHOLD
    updated = await mgr.update(0.5)

    conn = await hermetic_cm.get("memory.db")
    row = await (await conn.execute("SELECT value FROM preferences WHERE key=?", (mgr.key,))).fetchone()
    assert row is not None
    assert float(row[0]) == updated


async def test_legacy_rows_survive_migration(tmp_path, monkeypatch):
    """IF NOT EXISTS contract: a pre-existing legacy preferences table keeps its rows."""
    import sqlite3

    (tmp_path / "memory.db").touch()
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.execute("CREATE TABLE preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, value TEXT NOT NULL, updated_at REAL)")
    conn.execute("INSERT INTO preferences (key, value, updated_at) VALUES ('legacy_key', '42', 0)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()

    live = await connection_manager.get("memory.db")
    row = await (await live.execute("SELECT value FROM preferences WHERE key='legacy_key'")).fetchone()
    assert row is not None and row[0] == "42"
    connection_manager._conns.clear()
