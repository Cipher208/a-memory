"""A synchronous connection opened in a feature must not outlive the call.

`with sqlite3.connect(...)` is a TRANSACTION context manager: `__exit__` commits or
rolls back and leaves the connection open. In a script the garbage collector hides it;
in the autohooks daemon, which serves every event for hours, it is a leak of one
descriptor per call, and an unclosed connection holding a transaction holds the write
lock for every other writer on the base.

Measured before the fix: `with sqlite3.connect(...)` 50 times -> +50 descriptors with
gc disabled, and the live daemon held 29 descriptors on `memory.db` ten minutes after
starting. Both numbers are the same defect.

The tests count descriptors, and they disable the collector: without that, CPython
frees the connection on the last reference going away and the leak is invisible — a
test that passes on the broken code proves nothing.
"""

import gc
import os
import sqlite3

import pytest

from shared.sync_db import open_db


def _fds_on(path) -> int:
    """Descriptors this process holds on `path`."""
    target = str(path)
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}") == target:
                n += 1
        except OSError:
            continue
    return n


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "memory.db"
    with sqlite3.connect(str(p)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('seed')")
    yield p
    gc.enable()


def test_open_db_closes(db):
    """One use, one descriptor, gone on the way out."""
    gc.disable()
    try:
        before = _fds_on(db)
        with open_db(db) as conn:
            assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
            assert _fds_on(db) == before + 1, "the connection should be open inside the block"
        assert _fds_on(db) == before, "open_db must close the connection it opened"
    finally:
        gc.enable()


def test_repeated_uses_do_not_accumulate(db):
    """The defect: one descriptor per call, forever."""
    gc.disable()
    try:
        before = _fds_on(db)
        for _ in range(50):
            with open_db(db) as conn:
                conn.execute("SELECT 1").fetchone()
        assert _fds_on(db) == before, "50 uses left descriptors open"
    finally:
        gc.enable()


def test_a_builtin_context_manager_would_leak(db):
    """The control: the same loop on `with sqlite3.connect(...)` DOES grow.

    Without this test the assertion above could pass on a machine where the counter
    itself is broken, and the fix could be imaginary.
    """
    gc.disable()
    try:
        before = _fds_on(db)
        for _ in range(10):
            with sqlite3.connect(str(db)) as conn:
                conn.execute("SELECT 1").fetchone()
        assert _fds_on(db) == before + 10, "the built-in context manager is expected to leak"
    finally:
        gc.enable()


def test_commit_still_happens_on_a_clean_exit(db):
    """open_db must not turn every write into a lost write."""
    gc.disable()
    try:
        with open_db(db) as conn:
            conn.execute("INSERT INTO t (v) VALUES ('kept')")
    finally:
        gc.enable()

    with open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM t WHERE v='kept'").fetchone()[0] == 1


def test_rollback_on_exception(db):
    """An exception must not leave the row behind, and must not be swallowed."""
    gc.disable()
    try:
        with pytest.raises(RuntimeError), open_db(db) as conn:
            conn.execute("INSERT INTO t (v) VALUES ('discarded')")
            raise RuntimeError("boom")
    finally:
        gc.enable()

    with open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM t WHERE v='discarded'").fetchone()[0] == 0


def test_the_connection_is_closed_even_when_the_body_raises(db):
    gc.disable()
    try:
        before = _fds_on(db)
        with pytest.raises(RuntimeError), open_db(db) as conn:
            conn.execute("SELECT 1").fetchone()
            raise RuntimeError("boom")
        assert _fds_on(db) == before, "the descriptor must be released on the error path too"
    finally:
        gc.enable()


# --- the real hook path, not only the helper -------------------------------------
#
# `hooks/user_hooks.py` calls reflection, rehydrate and semantic_audit on every event,
# and the daemon serves events for hours. The helper being correct is not enough: every
# call site has to be on it. These tests call the functions the daemon calls.


@pytest.fixture
def feature_db(tmp_path, monkeypatch):
    from shared.connection import connection_manager

    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    p = tmp_path / "memory.db"
    with sqlite3.connect(str(p)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS core_memory (entry_id INTEGER PRIMARY KEY, user_id TEXT)")
    yield p
    gc.enable()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: __import__("features.disclosure", fromlist=["list_rules"]).list_rules("default"), id="disclosure"),
        pytest.param(lambda: __import__("features.procedures", fromlist=["proc_list"]).proc_list("default", "user"), id="procedures"),
        pytest.param(lambda: __import__("features.stash", fromlist=["stash_list"]).stash_list("default", "user"), id="stash"),
        pytest.param(lambda: __import__("features.scratchpad", fromlist=["read_entries"]).read_entries("default", "user"), id="scratchpad"),
        pytest.param(lambda: __import__("features.reflection", fromlist=["list_reflections"]).list_reflections("default"), id="reflection"),
        pytest.param(lambda: __import__("features.counterfactual", fromlist=["list_cfs"]).list_cfs("default"), id="counterfactual"),
    ],
)
def test_feature_calls_do_not_accumulate_descriptors(feature_db, call):
    """Twenty calls on a feature must leave the descriptor count where it started."""
    gc.disable()
    try:
        call()  # warm: table creation, first-run paths
        before = _fds_on(feature_db)
        for _ in range(20):
            call()
        grown = _fds_on(feature_db) - before
        assert grown == 0, f"20 calls left {grown} descriptor(s) open"
    finally:
        gc.enable()
