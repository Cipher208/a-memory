"""A1.4-a26: wiki_links exists in a freshly migrated DB.

WikiIndex.init_db() lazy-creates the table, but alembic-initialized
deployments (prod cowagent/hermes/mimocode) never ran it — add_link and
wiki_query BFS hit "no such table: wiki_links" (audit finding #4).
"""

import pytest

from shared.connection import AsyncConnectionManager, connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    manager = AsyncConnectionManager(base_dir=tmp_path)
    await MigrationManager(cm=manager).migrate()
    return manager


async def test_fresh_db_has_wiki_links(cm):
    conn = await cm.get("memory.db")
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_links'")
    assert await cur.fetchone() is not None


async def test_wiki_query_bfs_on_fresh_db(cm, monkeypatch):
    """The BFS tool path works against a migration-only schema (no init_db)."""
    import sqlite3

    monkeypatch.setattr(connection_manager, "base_dir", cm.base_dir)
    connection_manager._conns.clear()
    seed = sqlite3.connect(cm.base_dir / "memory.db")
    seed.execute(
        "INSERT INTO wiki_index (layer, wiki_type, title, file_path, content, created_at, updated_at, status) VALUES ('user','work_notes','a','/x/a.md','A','0','0','active')"
    )
    seed.execute(
        "INSERT INTO wiki_index (layer, wiki_type, title, file_path, content, created_at, updated_at, status) VALUES ('user','work_notes','b','/x/b.md','B','0','0','active')"
    )
    seed.execute("INSERT INTO wiki_links (layer, from_path, to_path, link_type, created_at) VALUES ('user','/x/a.md','/x/b.md','follows','0')")
    seed.commit()
    seed.close()

    from features.wiki_query import wiki_query_bfs

    res = await wiki_query_bfs("/x/a.md", depth=2, layer="user")
    assert any("b.md" in n["path"] for n in res["nodes"])
    connection_manager._conns.clear()
