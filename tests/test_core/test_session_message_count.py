"""close_session must persist message_count (Elli 16.09 D2 root cause).

The parameter was accepted but never written: every sessions row kept the
DEFAULT 0, so _pick_substantive's message_count-first ordering could never
differentiate and always fell back to recency.
"""

import sqlite3

import pytest

from core.session import SessionStore
from shared.connection import connection_manager


@pytest.fixture()
def tmp_base(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    connection_manager._conns.clear()
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    yield tmp_path
    connection_manager.base_dir = original
    connection_manager._conns.clear()


def _row_count(db_path, session_id):
    conn = sqlite3.connect(db_path / "memory.db")
    row = conn.execute("SELECT message_count FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    conn.close()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_close_session_persists_message_count(tmp_base):
    store = SessionStore()
    await store._init_db()
    sid = await store.create_session("default")
    await store.close_session(sid, summary="Real work", message_count=12)
    assert _row_count(tmp_base, sid) == 12


@pytest.mark.asyncio
async def test_service_run_stays_zero(tmp_base):
    store = SessionStore()
    await store._init_db()
    sid = await store.create_session("default")
    await store.close_session(sid, summary="cron probe session")
    assert _row_count(tmp_base, sid) == 0
