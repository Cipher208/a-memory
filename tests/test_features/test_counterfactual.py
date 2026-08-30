"""D1.20: counterfactual memory — what could have been."""

import sqlite3

import pytest

from shared.connection import connection_manager


@pytest.fixture
def cf_db(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS counterfactuals ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', anchor TEXT NOT NULL,"
            " premise TEXT NOT NULL, projection TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original


def test_save_and_list_by_anchor(cf_db):
    from features.counterfactual import save_cf, list_cfs

    cid = save_cf("u1", "deploy_flow", "if we had staged the migration", "no 3am rollback would have been needed")
    assert cid > 0
    save_cf("u1", "other_anchor", "if X", "then Y")
    rows = list_cfs("u1", anchor="deploy_flow")
    assert len(rows) == 1
    assert rows[0]["premise"].startswith("if we had")
    assert rows[0]["anchor"] == "deploy_flow"
    assert len(list_cfs("u1")) == 2
    assert list_cfs("u1", anchor="nomatch") == []
