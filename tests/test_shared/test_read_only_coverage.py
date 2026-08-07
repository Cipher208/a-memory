"""Tests for shared/read_only.py — essential behavior."""

import sqlite3

from shared.read_only import ReadOnlyReplica


def _create_source_db(path):
    path.mkdir(parents=True, exist_ok=True)
    db = path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE test (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO test VALUES (1, 'alice')")
    conn.commit()
    conn.close()
    return db


def test_sync_creates_replica(tmp_path):
    src = tmp_path / "source"
    _create_source_db(src)
    replica = ReadOnlyReplica(source_dir=str(src), replica_dir=str(tmp_path / "replica"))
    result = replica.sync()
    assert result.get("memory.db") == 1
    assert (tmp_path / "replica" / "memory.db").exists()


def test_get_conn_after_sync(tmp_path):
    src = tmp_path / "source"
    _create_source_db(src)
    replica = ReadOnlyReplica(source_dir=str(src), replica_dir=str(tmp_path / "replica"))
    replica.sync()
    conn = replica.get_conn()
    assert conn is not None


def test_start_stop_auto_sync(tmp_path):
    src = tmp_path / "source"
    _create_source_db(src)
    replica = ReadOnlyReplica(source_dir=str(src), replica_dir=str(tmp_path / "replica"))
    replica.start_auto_sync(interval_seconds=1)
    assert replica._running is True
    replica.stop()
    assert replica._running is False


def test_sync_updates_last_sync_time(tmp_path):
    src = tmp_path / "source"
    _create_source_db(src)
    replica = ReadOnlyReplica(source_dir=str(src), replica_dir=str(tmp_path / "replica"))
    assert replica._last_sync == 0.0
    replica.sync()
    assert replica._last_sync > 0.0
