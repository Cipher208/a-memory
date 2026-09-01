"""D2.5 — procedural memory minimal core: HOW-knowledge with execution stats."""

import pytest

from features import procedures as pr


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from shared.connection import connection_manager

    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    yield


def test_save_and_get(tmp_db):
    out = pr.proc_save("pu", "user", "deploy-ariel", ["git pull", "uv sync", "kill -HUP mainpid"], notes="master only")
    assert out == {"name": "deploy-ariel", "steps": 3}

    got = pr.proc_get("pu", "user", "deploy-ariel")
    assert got["name"] == "deploy-ariel"
    assert got["steps"] == ["git pull", "uv sync", "kill -HUP mainpid"]
    assert got["notes"] == "master only"
    assert got["times_used"] == 0 and got["success_rate"] == 0.0


def test_save_validation(tmp_db):
    with pytest.raises(ValueError, match="invalid procedure name"):
        pr.proc_save("pu", "user", "BAD NAME", ["step"])
    with pytest.raises(ValueError, match="steps must be a non-empty list"):
        pr.proc_save("pu", "user", "ok-name", [])
    with pytest.raises(ValueError, match="steps must be a non-empty list"):
        pr.proc_save("pu", "user", "ok-name", ["", "  "])
    pr.proc_save("pu", "user", "dup", ["s1"])
    with pytest.raises(ValueError, match="already exists"):
        pr.proc_save("pu", "user", "dup", ["s2"])


def test_list_is_payload_free_and_computes_rate(tmp_db):
    pr.proc_save("pu", "user", "p1", ["a", "b"])
    pr.proc_use("pu", "user", "p1", success=True)
    pr.proc_use("pu", "user", "p1", success=False)
    listed = pr.proc_list("pu", "user")
    assert len(listed) == 1
    row = listed[0]
    assert row["name"] == "p1" and "steps_json" not in row and "steps" not in row
    assert row["times_used"] == 2 and row["times_succeeded"] == 1 and row["success_rate"] == 0.5


def test_use_updates_counters_and_appends_learned(tmp_db):
    pr.proc_save("pu", "user", "p1", ["a"], notes="base")
    out = pr.proc_use("pu", "user", "p1", success=True, learned="uv sync first")
    assert out["times_used"] == 1 and out["times_succeeded"] == 1 and out["success_rate"] == 1.0
    pr.proc_use("pu", "user", "p1", success=False)
    got = pr.proc_get("pu", "user", "p1")
    assert got["times_used"] == 2 and got["times_succeeded"] == 1 and got["success_rate"] == 0.5
    assert got["notes"] == "base; learned: uv sync first"


def test_missing_name_raises_and_delete(tmp_db):
    with pytest.raises(ValueError, match="not found"):
        pr.proc_get("pu", "user", "ghost")
    with pytest.raises(ValueError, match="not found"):
        pr.proc_use("pu", "user", "ghost", success=True)
    pr.proc_save("pu", "user", "p1", ["a"])
    assert pr.proc_delete("pu", "user", "p1") == {"name": "p1", "deleted": True}
    assert pr.proc_delete("pu", "user", "p1") == {"name": "p1", "deleted": False}


def test_steering_route_covers_procedures(tmp_db):
    from features.steering import steering_hints

    hints = steering_hints("как делать деплой процедуру")
    assert hints, "no hints matched"
    assert any("memory_procedure" in h["use"] for h in hints)
    # wiki route remains reachable for rich skill docs
    hints2 = steering_hints("how do i write a skill doc")
    assert any("wiki_search" in h["use"] for h in hints2)
