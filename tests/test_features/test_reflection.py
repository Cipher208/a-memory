"""D1.16: reflection system — deterministic meta-memories."""

import sqlite3
import time
from types import SimpleNamespace

import pytest

from shared.connection import connection_manager


@pytest.fixture
def reflections_db(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reflections ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL, layer TEXT NOT NULL DEFAULT 'user',"
            " topic TEXT, insight TEXT NOT NULL, stats_json TEXT,"
            " created_at REAL NOT NULL)"
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original


def test_build_reflection_counts_and_topics(reflections_db):
    from features.reflection import build_reflection

    mem = SimpleNamespace(
        l3=SimpleNamespace(
            get_episodes=lambda user_id, limit=20, offset=0: [
                SimpleNamespace(summary="deploy ariel to server now", created_at=time.time()),
                SimpleNamespace(summary="deploy again tomorrow", created_at=time.time()),
            ],
        ),
    )
    out = build_reflection(mem, "u1", period_hours=24)
    assert "deploy" in out["stats"]["top_topics"]
    assert out["stats"]["episodes_window"] == 2
    assert "insight" in out and len(out["insight"]) > 40


def test_save_and_list_reflections(reflections_db):
    from features.reflection import save_reflection, list_reflections

    sid = save_reflection("u1", topic="weekly", insight="Things repeat: deploy.", stats={"a": 1})
    assert sid > 0
    rows = list_reflections("u1", topic="weekly", limit=5)
    assert len(rows) == 1 and rows[0]["insight"].startswith("Things repeat")
    assert list_reflections("u1", topic="nomatch", limit=5) == []


def test_nightly_reflection_phase_writes_row(reflections_db):
    from features.reflection import nightly_reflection, list_reflections

    mem = SimpleNamespace(l3=SimpleNamespace(search_by_tag=lambda *a, **k: [], get_episodes=lambda *a, **k: []))
    res = nightly_reflection(mem, "u1")
    assert res["written"] is True
    rows = list_reflections("u1", topic="daily", limit=5)
    assert len(rows) == 1
