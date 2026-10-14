"""A migration that has nothing to apply must not touch the base.

Why this is a defect and not an optimisation.

`lifespan` runs `migrate()` **before** the server answers anything, and every
process serving the base runs its own lifespan. `migrate()` called
`alembic_command.upgrade(cfg, "head")` unconditionally — so every start opened a
write transaction merely to discover that the version was already current.

Measured on the live hermes base, 14.10.2026: the MCP server started eleven
times in one day, and sessions failed with
`Failed to connect to MCP server 'ariel-memory': CancelledError` in four of them
(12.10 16:03, 13.10 09:02, 14.10 07:02 and 08:02). The server itself was healthy
and logged its start five seconds in — the client had already given up waiting
for a migration that was queued behind another process's write.

The fix is to read the version, compare it to the script heads, and only then
run Alembic. The comparison needs no database connection, so the steady state
does no write at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command as alembic_command
from alembic.script import ScriptDirectory

from shared.migrations import MigrationManager


def _head() -> str:
    """The repository's current Alembic head, read from the scripts, not the base."""
    cfg = MigrationManager()._get_alembic_config()
    return ScriptDirectory.from_config(cfg).get_current_head()


def test_migrate_skips_alembic_when_already_at_head(monkeypatch) -> None:
    """The steady state must not open a write transaction.

    This is the whole point: the failed sessions were waiting on a write that
    had nothing to write.
    """
    calls: list[tuple] = []
    monkeypatch.setattr(alembic_command, "upgrade", lambda *a, **k: calls.append(a))

    manager = MigrationManager()
    monkeypatch.setattr(manager, "get_current_version", lambda: asyncio.sleep(0, result=_head()))

    result = asyncio.run(manager.migrate())

    assert calls == [], "alembic ran even though the base was already at head"
    assert result["status"] == "up_to_date"
    assert result["new_version"] == _head()


def test_migrate_still_runs_when_the_base_is_behind(monkeypatch) -> None:
    """The other half: a base that really is behind must still be migrated."""
    calls: list[tuple] = []
    monkeypatch.setattr(alembic_command, "upgrade", lambda *a, **k: calls.append(a))

    versions = iter(["0000_ancient", _head()])
    manager = MigrationManager()
    monkeypatch.setattr(manager, "get_current_version", lambda: asyncio.sleep(0, result=next(versions)))

    result = asyncio.run(manager.migrate())

    assert len(calls) == 1, "a base behind head must still be upgraded"
    assert result["new_version"] == _head()


def test_migrate_runs_when_there_is_no_version_yet(monkeypatch) -> None:
    """An empty base has no version row, so there is nothing to compare to."""
    calls: list[tuple] = []
    monkeypatch.setattr(alembic_command, "upgrade", lambda *a, **k: calls.append(a))

    versions = iter([None, _head()])
    manager = MigrationManager()
    monkeypatch.setattr(manager, "get_current_version", lambda: asyncio.sleep(0, result=next(versions)))

    asyncio.run(manager.migrate())

    assert len(calls) == 1, "an uninitialised base must be migrated, not skipped"


def test_alembic_config_points_at_the_repo_scripts() -> None:
    """A guard on the guard: the head must come from this repo's scripts."""
    cfg = MigrationManager()._get_alembic_config()
    script_location = Path(cfg.get_main_option("script_location") or "")
    assert script_location.name == "alembic", script_location
    assert script_location.is_dir(), f"alembic script dir missing: {script_location}"
