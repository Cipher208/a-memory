"""The cleanup tool must not write while another maintenance pass holds the base.

`maintenance_lock` exists so that one writer at a time touches a base: `lifespan`,
`importance_scheduler`, `forgetting_ritual` and `backup_cron` all take it. The
`memory_cleanup` tool did not — and it is the heaviest writer in the house: six
operations dispatched concurrently by `asyncio.gather`, one of which writes
`core_memory` through the forgetting path.

Measured context (17.10.2026): `Decay failed` on the hermes base repeats every 15
minutes with intervals of 915 s against a 900 s cycle — the extra 15 s is exactly
`BUSY_TIMEOUT_MS`, which means a real writer held the file longer than the timeout.
Whatever that writer is, a tool that writes on demand without asking for the lock is
one of the candidates, and it is the only writer in the house that skips the lock
entirely.

The tests hold the lock themselves and assert the tool does not start its writes —
then release it and assert the writes do happen. Assertions are on a seam
(`_run_cleanup_writes`), because that is precisely the boundary the lock must guard.
"""

import asyncio

import pytest

from shared import maintenance_lock


@pytest.fixture
def base_dir(tmp_path, monkeypatch):
    """Point the connection manager at a temp base and make it the lock directory."""
    from shared.connection import connection_manager

    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    return tmp_path


def test_cleanup_waits_instead_of_writing_while_another_pass_holds_the_lock(base_dir, monkeypatch):
    """A held lock must keep the tool's writes from starting."""
    from mcp_server.tools import ops

    started: list[str] = []

    async def fake_writes(user_id, retention_days, archive_dir):
        started.append(user_id)
        return {"dedup_core": 0}

    monkeypatch.setattr(ops, "_run_cleanup_writes", fake_writes)
    monkeypatch.setattr(ops, "TOOL_MAINTENANCE_WAIT_S", 0.3)

    holder = maintenance_lock.acquire(base_dir)
    assert holder is not None

    async def t():
        return await ops.memory_cleanup(user_id="default", retention_days=30)

    try:
        result = asyncio.run(t())
    finally:
        maintenance_lock.release(holder)

    assert started == [], "the tool wrote while another pass held the maintenance lock"
    assert result.get("status") == "busy", f"the refusal must be visible, not silent: {result}"


def test_cleanup_proceeds_when_the_lock_is_free(base_dir, monkeypatch):
    """The lock must not become a way for cleanup to never run."""
    from mcp_server.tools import ops

    started: list[str] = []

    async def fake_writes(user_id, retention_days, archive_dir):
        started.append(user_id)
        return {"dedup_core": 0}

    monkeypatch.setattr(ops, "_run_cleanup_writes", fake_writes)
    monkeypatch.setattr(ops, "TOOL_MAINTENANCE_WAIT_S", 0.3)

    async def t():
        return await ops.memory_cleanup(user_id="default", retention_days=30)

    result = asyncio.run(t())

    assert started == ["default"], "cleanup must run when nothing else holds the lock"
    assert result.get("status") != "busy", result


def test_cleanup_releases_the_lock_when_its_writes_raise(base_dir, monkeypatch):
    """A failure inside the writes must not leave the base locked for everyone else."""
    from mcp_server.tools import ops

    async def explode(user_id, retention_days, archive_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(ops, "_run_cleanup_writes", explode)
    monkeypatch.setattr(ops, "TOOL_MAINTENANCE_WAIT_S", 0.3)

    async def t():
        with pytest.raises(RuntimeError):
            await ops.memory_cleanup(user_id="default", retention_days=30)

    asyncio.run(t())

    # If the lock leaked, this acquire returns None and the next pass never runs.
    fd = maintenance_lock.acquire(base_dir)
    assert fd is not None, "the maintenance lock was not released on the error path"
    maintenance_lock.release(fd)


def test_purge_takes_the_lock_too(base_dir, monkeypatch):
    """`memory_lucidity_purge` is the other tool that writes in parallel without it."""
    from mcp_server.tools import ops

    started: list[str] = []

    async def fake_purge(app, user_id, cutoff):
        started.append(user_id)
        return {"core_memory": 0}

    monkeypatch.setattr(ops, "_run_purge_writes", fake_purge)
    monkeypatch.setattr(ops, "_get_ctx", lambda ctx: object())
    monkeypatch.setattr(ops, "TOOL_MAINTENANCE_WAIT_S", 0.3)

    holder = maintenance_lock.acquire(base_dir)
    assert holder is not None

    async def t():
        return await ops.memory_lucidity_purge(user_id="default", hours=24, ctx=None)

    try:
        result = asyncio.run(t())
    finally:
        maintenance_lock.release(holder)

    assert started == [], "purge wrote while another pass held the maintenance lock"
    assert result.get("status") == "busy", f"the refusal must be visible: {result}"
