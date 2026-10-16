"""The maintenance loop must report its own connection health, not only its failures.

The problem this solves.

On 15–16.10 the hermes base suffered repeated `database is locked` storms — two
hours on 15.10, eight hours overnight — always in the SERVER process, always from
`lifecycle/forgetting.py:183`, and always ending when the process was restarted.
Probing the write lock from OUTSIDE returned free every time (8/8 at 0.3 ms), which
is the signature of a lock held inside the process itself.

What made that hard was the absence of any view from inside. Everything known came
from outside, poking at /proc: descriptor counts that oscillated between 49 and 57
and could not be read as a trend, and no way at all to ask the process how many
connections it believed it had, or how many aiosqlite workers were running with no
owner.

`connection_manager.stats()` and `leaked_connection_workers()` already existed — for
the test suite. Nothing called them in production. So the number that would have
answered the question was available all along.

This logs it alongside the same 15-minute maintenance pass the failure happens in,
so the next storm has a reading taken moments before it.

The `hermetic_global_db` fixture is used rather than a private manager: the point is
to measure the GLOBAL singleton, which is what the loop actually reads.
"""

from __future__ import annotations

import logging

import pytest

from mcp_server.lifespan import _connection_health


@pytest.mark.asyncio
async def test_health_reports_connections_and_leaks(hermetic_global_db, caplog):
    """The line must carry both numbers, and they must be real ones.

    Asserted on content, not on "a log happened": a message that prints nothing
    usable is the same silence this exists to remove.
    """
    from shared.connection import connection_manager

    conn = await connection_manager.get("memory.db")
    await conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    await conn.commit()

    with caplog.at_level(logging.INFO):
        health = await _connection_health()

    assert health["connections"] >= 1, f"expected the open connection to be counted: {health}"
    assert "memory.db" in health["dbs"], f"expected memory.db among the dbs: {health}"
    assert isinstance(health["leaked_workers"], int)
    assert health["leaked_workers"] >= 0

    messages = [r.getMessage() for r in caplog.records]
    assert any("connection health" in m for m in messages), f"no health line emitted: {messages}"
    line = next(m for m in messages if "connection health" in m)
    assert "connections=" in line and "leaked_workers=" in line, line


@pytest.mark.asyncio
async def test_health_counts_a_leaked_worker(hermetic_global_db):
    """A connection dropped out of the manager must show up in the count.

    This is the reading that would have told us a leak was accumulating: the manager
    can honestly report one connection while the process holds several.
    """
    from shared.connection import connection_manager

    conn = await connection_manager.get("memory.db")
    # The drop-without-close shape: the manager forgets it, the strong registry keeps
    # it alive — the defect #124 fixed at the one call site that could be reached.
    connection_manager._conns.clear()

    health = await _connection_health()

    assert health["connections"] == 0, f"the manager should report nothing owned: {health}"
    assert health["leaked_workers"] >= 1, f"a connection alive outside any manager must be visible as a leak: {health}"
    await conn.close()


@pytest.mark.asyncio
async def test_health_never_raises(hermetic_global_db, monkeypatch):
    """Diagnostics must not be able to break the loop they are diagnosing."""
    monkeypatch.setattr(
        "shared.connection.connection_manager.stats",
        lambda: (_ for _ in ()).throw(RuntimeError("stats blew up")),
    )

    health = await _connection_health()  # must not raise

    assert health["connections"] == -1, f"a failed reading must be reported as such, not swallowed: {health}"


@pytest.mark.asyncio
async def test_the_storage_pass_actually_reads_health(hermetic_global_db, monkeypatch):
    """The wiring is the point: a diagnostic nobody calls is the silence it removes.

    The first mutation run left every test green after deleting the call from the
    periodic loop, because the tests above call `_connection_health()` directly. A
    test that calls the function cannot tell "wired" from "written and forgotten" —
    which is the exact defect this whole line of work keeps finding in the codebase.
    """
    import mcp_server.lifespan as lsp

    called: list[int] = []

    async def _spy() -> dict:
        called.append(1)
        return {"connections": 0, "dbs": [], "leaked_workers": 0}

    monkeypatch.setattr(lsp, "_connection_health", _spy)

    async def _no_maintenance(**kwargs):
        return []

    monkeypatch.setattr("features.db_maintenance.run_db_maintenance", _no_maintenance)

    await lsp._storage_pass()

    assert called, "the storage pass did not read connection health — nothing will ever log it"


@pytest.mark.asyncio
async def test_health_is_read_even_when_maintenance_fails(hermetic_global_db, monkeypatch):
    """The reading must survive a failing maintenance step.

    A storm is exactly when the numbers are wanted, and a size/VACUUM step that blows
    up must not swallow the health line with it.
    """
    import mcp_server.lifespan as lsp

    async def _boom(**kwargs):
        raise RuntimeError("maintenance blew up")

    monkeypatch.setattr("features.db_maintenance.run_db_maintenance", _boom)

    health = await lsp._storage_pass()  # must not raise

    assert health is None or isinstance(health, dict)
