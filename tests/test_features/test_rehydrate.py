"""D3.5: compaction drift log + rehydrate lookups."""

import sqlite3

import pytest

from shared.connection import connection_manager


@pytest.fixture
def compaction_db(tmp_path, monkeypatch):
    """Redirect base_dir to a scratch dir with the compaction_events table; restore after."""
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS compaction_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL,"
            " old_session_id TEXT,"
            " new_session_id TEXT,"
            " reason TEXT,"
            " summary TEXT,"
            " created_at REAL NOT NULL)"
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original


def test_log_and_read_back(compaction_db):
    from features.rehydrate import log_compaction, recent_compaction

    assert log_compaction("u1", old_session_id="s-old", new_session_id="s-new", reason="compaction") is True
    row = recent_compaction("u1", window_hours=6.0)
    assert row is not None
    assert row["old_session_id"] == "s-old"
    assert row["reason"] == "compaction"
    assert recent_compaction("other", window_hours=6.0) is None


def test_summary_clamped(compaction_db):
    from features.rehydrate import log_compaction, recent_compaction

    log_compaction("u1", summary="x" * 3000)
    assert len(recent_compaction("u1", 1.0)["summary"]) == 2000


def test_stale_outside_window(compaction_db):
    import time

    from features.rehydrate import log_compaction, recent_compaction

    log_compaction("u1")
    with sqlite3.connect(str(compaction_db)) as conn:
        conn.execute("UPDATE compaction_events SET created_at = ?", (time.time() - 7 * 3600,))
        conn.commit()
    assert recent_compaction("u1", 6.0) is None


def test_missing_table_degrades(compaction_db):
    from features.rehydrate import log_compaction, recent_compaction

    with sqlite3.connect(str(compaction_db)) as conn:
        conn.execute("DROP TABLE compaction_events")
        conn.commit()
    assert log_compaction("u1") is False
    assert recent_compaction("u1", 1.0) is None
