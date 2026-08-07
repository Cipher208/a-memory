"""Tests for saga behavior — compensation, retry, idempotency."""

import asyncio

import pytest

import shared.connection as _conn_mod
from shared.saga import Saga

# ── Compensation ──


def test_compensation_rolls_back():
    from core import memory_manager

    async def t():
        mm = memory_manager
        await mm.user_memory("saga_c").remember("k1", "v1", 0.9)

        async def step1(d):
            await mm.user_memory("saga_c").remember("k2", "v2", 0.8)
            return {"ok": True}

        async def fail(d):
            raise RuntimeError("boom")

        async def compensate(d):
            await mm.user_memory("saga_c").forget("k2")

        saga = Saga("comp")
        saga.add_step("s1", step1, compensate)
        saga.add_step("s2", fail)
        try:
            await saga.execute()
        except RuntimeError:
            pass

        assert len(await mm.user_memory("saga_c").recall("k2")) == 0
        assert len(await mm.user_memory("saga_c").recall("k1")) > 0

    asyncio.run(t())


def test_success_no_compensation():
    called = False

    async def t():
        nonlocal called

        async def compensate(d):
            nonlocal called
            called = True

        saga = Saga("ok")
        saga.add_step("s", lambda d: {"r": 1}, compensate)
        await saga.execute()
        assert not called
        assert saga.status.value == "completed"

    asyncio.run(t())


def test_nested_saga():
    executed = []

    async def t():
        async def inner(d):
            executed.append("inner")
            return {"i": True}

        async def outer(d):
            executed.append("outer")
            return {"o": True}

        inner_saga = Saga("inner")
        inner_saga.add_step("s", inner)

        outer_saga = Saga("outer")
        outer_saga.add_step("s", outer)
        outer_saga.add_step("nested", inner_saga)

        result = await outer_saga.execute()
        assert result["i"] is True
        assert result["o"] is True

    asyncio.run(t())


# ── Retry ──


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient():
    call_count = {"n": 0}

    async def flaky(d):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ConnectionError("boom")
        return {"v": 42}

    saga = Saga("flaky", timeout_seconds=30)
    saga.add_step("s", flaky, retry_attempts=3, retry_backoff=0.01, retry_on=(ConnectionError,))
    result = await saga.execute({})
    assert result["v"] == 42
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_retry_gives_up():
    async def always_fail(d):
        raise TimeoutError("nope")

    saga = Saga("fail")
    saga.add_step("s", always_fail, retry_attempts=2, retry_backoff=0.01)
    with pytest.raises(TimeoutError):
        await saga.execute({})


@pytest.mark.asyncio
async def test_retry_compensates():
    compensated = []

    async def succeed(d):
        return {"ok": True}

    async def fail(d):
        raise ConnectionError("down")

    async def undo(d):
        compensated.append("undo")

    saga = Saga("comp_retry")
    saga.add_step("s1", succeed, compensation=undo)
    saga.add_step("s2", fail, retry_attempts=1, retry_backoff=0.01)
    with pytest.raises(ConnectionError):
        await saga.execute({})
    assert "undo" in compensated


# ── Idempotency ──


@pytest.mark.asyncio
async def test_idempotent_replay(tmp_path):
    from shared.connection import AsyncConnectionManager

    m = AsyncConnectionManager(base_dir=str(tmp_path))
    await m.execute_script(
        "memory.db",
        """
        CREATE TABLE IF NOT EXISTS saga_step_log (
            saga_id TEXT NOT NULL, step_name TEXT NOT NULL, params_hash TEXT NOT NULL,
            result_json BLOB, completed_at REAL NOT NULL,
            PRIMARY KEY (saga_id, step_name, params_hash)
        ) WITHOUT ROWID
    """,
    )
    old_cm = _conn_mod.connection_manager
    _conn_mod.connection_manager = m

    try:
        count = {"n": 0}

        async def step(d):
            count["n"] += 1
            return {"w": True}

        def key_fn(d):
            return f"user:{d.get('user_id', 'x')}"

        saga = Saga("idem", timeout_seconds=10)
        saga.add_step("s", step, idempotency_key_fn=key_fn)
        await saga.execute({"user_id": "alice"})
        assert count["n"] == 1

        same = Saga("idem", saga_id=saga.saga_id, timeout_seconds=10)
        same.add_step("s", step, idempotency_key_fn=key_fn)
        await same.execute({"user_id": "alice"})
        assert count["n"] == 1
    finally:
        _conn_mod.connection_manager = old_cm
