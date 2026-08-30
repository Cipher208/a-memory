"""D1.7 memory_query — whitelisted-filter parameterized SQL over core/episodes."""

import sqlite3
import time

import pytest

from shared.connection import connection_manager


@pytest.fixture
async def query_db(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    await connection_manager.close_all()
    db = tmp_path / "memory.db"
    now = time.time()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE core_memory ("
            " entry_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', key TEXT NOT NULL, value TEXT NOT NULL,"
            " importance REAL DEFAULT 0.5, memory_kind TEXT, expires_at REAL,"
            " source TEXT DEFAULT 'manual', metadata TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE episodes ("
            " episode_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', summary TEXT NOT NULL,"
            " emotional_weight REAL DEFAULT 0.5, tags TEXT, memory_kind TEXT, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO core_memory (user_id, key, value, importance, created_at, updated_at) VALUES ('u1', 'deploy:venv', 'ssh then uv sync', 0.9, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO core_memory (user_id, key, value, importance, created_at, updated_at) VALUES ('u1', 'pizza', 'likes pineapple', 0.3, ?, ?)",
            (now - 86400, now - 86400),
        )
        conn.execute(
            "INSERT INTO episodes (user_id, summary, emotional_weight, tags, created_at) VALUES ('u1', 'release shipped', 0.7, '[\"auto_save\",\"release\"]', ?)",
            (now,),
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original
    await connection_manager.close_all()


@pytest.mark.asyncio
async def test_core_importance_and_like_filters(query_db):
    from features.query_dsl import query_memory

    res = await query_memory("u1", importance_min=0.5)
    assert res["count"] == 1 and res["rows"][0]["key"] == "deploy:venv"
    res = await query_memory("u1", key_like="deploy")
    assert res["count"] == 1
    res = await query_memory("u1", content_like="pineapple")
    assert res["count"] == 1 and res["rows"][0]["key"] == "pizza"


@pytest.mark.asyncio
async def test_created_window(query_db):
    from features.query_dsl import query_memory

    res = await query_memory("u1", created_since=time.time() - 3600)
    assert res["count"] == 1 and res["rows"][0]["key"] == "deploy:venv"


@pytest.mark.asyncio
async def test_episodes_tag_filter(query_db):
    from features.query_dsl import query_memory

    res = await query_memory("u1", source="episodes", tag="release")
    assert res["count"] == 1 and "release shipped" in res["rows"][0]["summary"]
    assert res["rows"][0]["tags"] == ["auto_save", "release"]


@pytest.mark.asyncio
async def test_unknown_source_and_core_tag_rejected(query_db):
    from features.query_dsl import query_memory

    with pytest.raises(ValueError):
        await query_memory("u1", source="wiki")
    with pytest.raises(ValueError):
        await query_memory("u1", tag="release")  # core has no tags


@pytest.mark.asyncio
async def test_like_with_quotes_is_safe(query_db):
    from features.query_dsl import query_memory

    res = await query_memory("u1", key_like="'; DROP TABLE core_memory; --")
    assert res["count"] == 0
    with sqlite3.connect(str(query_db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM core_memory").fetchone()[0]
    assert n == 2
