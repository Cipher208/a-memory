"""Shared fixtures for all tests."""

import gc
import os
import tempfile
from pathlib import Path

# Disable backup_cron before any imports to prevent daemon threads
os.environ["BACKUP_CRON_DISABLED"] = "1"
# Deterministic + fast: never load sentence-transformers in tests even when
# the optional extra is installed locally.
os.environ["ARIEL_HASH_EMBEDDINGS"] = "1"

import pytest


@pytest.fixture(autouse=True, scope="session")
def master_key_env():
    """Set master key for encryption across all tests."""
    os.environ["MCP_MASTER_KEY"] = "test-secret-for-unit-tests-only"
    from features import secrets

    secrets._master_cache.clear()
    yield
    os.environ.pop("MCP_MASTER_KEY", None)
    gc.collect()


@pytest.fixture(autouse=True, scope="session")
def hermetic_global_db():
    """Redirect the GLOBAL connection_manager to a session temp dir.

    Modules import the singleton directly (`from shared.connection import
    connection_manager`), so tests that construct managers without an explicit
    cm (adaptive_threshold, DreamBuffer, ConsolidationEngine...) would
    otherwise read/write the real ~/.mcp-ariel-memory data dir. Mutating the
    singleton in place keeps those references valid.
    """
    from shared.connection import connection_manager

    session_dir = tempfile.mkdtemp(prefix="ariel-test-global-")
    original_dir = connection_manager.base_dir
    connection_manager.base_dir = Path(session_dir)
    connection_manager._conns.clear()  # drop any already-open real-dir handles

    # Adaptive EMA caches a value on the instance; start fresh in the tmp dir.
    try:
        from shared.adaptive import adaptive_threshold

        adaptive_threshold._current_value = None
    except Exception:
        pass

    yield

    try:
        import asyncio

        asyncio.run(connection_manager.close_all())
    except Exception:
        pass
    connection_manager.base_dir = original_dir
    connection_manager._conns.clear()


@pytest.fixture(autouse=True)
def deterministic_gate_and_registry():
    """Deterministic importance gate + clean hook registry per test.

    - adaptive_threshold is a module singleton: gate() reads the cached EMA
      value and persists updates to the shared preferences DB. Without a
      reset the threshold drifts with execution order (high-importance tests
      push later low-importance saves below the gate — the dir-isolated
      test_mcp order-dependent failures). Pin it to the DEFAULT so every
      test sees identical gate semantics; tests exercising EMA logic
      monkeypatch it explicitly (test_hypothesis pattern).
    - AppContext() (used by several test files, e.g. test_post_session_diff)
      registers REAL UserHooks/AgentHooks into the GLOBAL hook_registry and
      never unregisters — handlers then leak into later tests. Snapshot and
      restore the registry around every test.
    """
    from hooks.registry import hook_registry
    from shared.adaptive import adaptive_threshold

    saved_handlers = {k: list(v) for k, v in hook_registry._hooks.items()}
    adaptive_threshold._current_value = adaptive_threshold.DEFAULT_THRESHOLD
    yield
    hook_registry._hooks.clear()
    hook_registry._hooks.update(saved_handlers)
    adaptive_threshold._current_value = None


@pytest.fixture(autouse=True)
def reap_leaked_workers():
    """Stop aiosqlite workers leaked by clear()-style teardowns after each test.

    Stopping right after the test (while its event loop is still alive)
    makes the stop sentinel fully effective; a single end-of-session sweep
    left a few stubborn workers parked in tx.get() with no live loop.
    """
    yield
    from shared.connection import leaked_connection_workers

    leaked_connection_workers(stop=True)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Force-stop every live aiosqlite worker so shutdown doesn't hang.

    aiosqlite worker threads are non-daemon; fixtures that drop handles via
    _conns.clear() leave them parked in queue.get() forever, hanging
    interpreter shutdown. Historically this file called os._exit(0), which
    also swallowed failure output and exit codes. Now: force-stop all
    tracked workers (per-test reaping already handled fixture leaks; this
    catches connections still owned by surviving managers), then let
    pytest exit normally with the real exit status.
    """
    from shared.connection import leaked_connection_workers

    leaked_connection_workers(stop=True, force=True)
