"""Tests for shared/connection.py — remaining unit tests."""

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _uid():
    return uuid.uuid4().hex[:8]


def test_connection_rollback():
    """rollback() should undo uncommitted changes."""
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir="/tmp/test_conn")
        conn = await cm.get(f"rollback_{_uid()}.db")
        await conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        await conn.execute("INSERT INTO t VALUES (1)")
        await conn.rollback()
        cur = await conn.execute("SELECT COUNT(*) FROM t")
        row = await cur.fetchone()
        assert row[0] == 0

    asyncio.run(t())


def test_new_db_pragmas(tmp_path):
    """A2.6: fresh DB gets page_size=16384 + auto_vacuum=INCREMENTAL."""
    import asyncio

    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        conn = await cm.get(f"pragmas_{_uid()}.db")
        cur = await conn.execute("PRAGMA page_size")
        assert (await cur.fetchone())[0] == 16384
        cur = await conn.execute("PRAGMA auto_vacuum")
        assert (await cur.fetchone())[0] == 2  # INCREMENTAL

    asyncio.run(t())


def test_composite_indexes_exist(tmp_path):
    """A2.9: hot-query composite indexes are created on init."""
    import asyncio

    from core.episodic import EpisodicMemory
    from core.memory import CoreMemory
    from core.session import SessionStore
    from features.audit_trail import AuditTrail
    from graph.epistemic import EpistemicGraph
    from shared.archived_memories import ArchivedMemories
    from shared.connection import AsyncConnectionManager
    from shared.dream_buffer import DreamBuffer
    from wiki.index import WikiIndex

    EXPECTED = {
        "idx_core_importance": "core_memory(layer, user_id, importance DESC)",
        "idx_episodes_layer_time": "episodes(layer, user_id, created_at DESC)",
        "idx_epi_scope_conf": "epi_nodes(layer, user_id, node_type, confidence DESC)",
        "idx_sessions_user_time": "sessions(user_id, started_at DESC)",
        "idx_audit_user_action": "audit_log(user_id, action, timestamp DESC)",
        "idx_wiki_layer_type_time": "wiki_index(layer, wiki_type, updated_at DESC)",
        "idx_archived_user": "archived_memories(user_id, archived_at DESC)",
        "idx_staging_layer_user_session": "staging_memories(layer, user_id, session_id, created_at)",
    }

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        await CoreMemory(cm=cm)._init_db()
        await EpisodicMemory(cm=cm)._init_db()
        await EpistemicGraph(cm=cm).init_db()
        await SessionStore(cm=cm)._init_db()
        await AuditTrail(cm=cm)._init_db()
        await WikiIndex(cm, layer="user").init_db()
        await ArchivedMemories(cm=cm)._init_db()
        await DreamBuffer(cm=cm)._init_db()

        conn = await cm.get("memory.db")
        for name in EXPECTED:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
            )
            assert await cur.fetchone() is not None, f"missing index {name}"

    asyncio.run(t())


def test_connection_stale_reopen():
    """Stale connection should be reopened automatically."""
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir="/tmp/test_conn")
        name = f"stale_{_uid()}.db"
        conn1 = await cm.get(name)
        await conn1.close()
        cm._conns.pop(name, None)
        conn2 = await cm.get(name)
        assert conn2 is not None
        cur = await conn2.execute("SELECT 1")
        row = await cur.fetchone()
        assert row[0] == 1

    asyncio.run(t())
