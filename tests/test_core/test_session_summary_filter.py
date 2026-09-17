"""Service sessions are labeled, never silent, never posing as work.

Elli 16.09 D5: the audit row led every recall block as "last session".
Rule (single source: is_service_session): zero-message runs surface with a
[service] tag; substantive rows stay untagged.
"""

import time

import pytest

from core.session import SessionStore
from shared.connection import connection_manager


@pytest.fixture
def tmp_base(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    connection_manager._conns.clear()
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    yield tmp_path
    connection_manager.base_dir = original
    connection_manager._conns.clear()


async def _close(store, user_id, summary, message_count, started):
    await store._init_db()
    sid = await store.create_session(user_id)
    await store.close_session(sid, summary=summary, message_count=message_count)
    # close_session scores but message_count is set via UPDATE below only in
    # legacy tests; current code persists it — assert through the real path.
    conn = await store._cm.get("memory.db")
    await conn.execute(
        "UPDATE sessions SET started_at=?, ended_at=? WHERE session_id=?",
        (started, started + 60.0, sid),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_service_row_labeled_work_row_plain(tmp_base):
    store = SessionStore()
    now = time.time()
    await _close(store, "default", "Real work session", 10, now - 3600)
    await _close(store, "default", "Memory audit session — no user interaction, diagnostic only", 0, now)
    summary = await store.get_session_summary("default")
    assert "- [service] Memory audit session" in summary
    assert "- Real work session" in summary


@pytest.mark.asyncio
async def test_only_service_rows_labeled_not_silenced(tmp_base):
    store = SessionStore()
    await _close(store, "default", "Memory audit session — no user interaction, diagnostic only", 0, time.time())
    summary = await store.get_session_summary("default")
    assert "[service]" in summary
    assert "Memory audit" in summary


@pytest.mark.asyncio
async def test_empty_base_returns_sentinel(tmp_base):
    store = SessionStore()
    await store._init_db()
    assert (await store.get_session_summary("default")).strip() == "No sessions yet."
