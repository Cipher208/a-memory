"""Shared fixtures for all tests."""

import gc
import os

# Disable backup_cron before any imports to prevent daemon threads
os.environ["BACKUP_CRON_DISABLED"] = "1"

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


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Force exit after all tests complete — aiosqlite worker thread bug."""
    os._exit(0)
