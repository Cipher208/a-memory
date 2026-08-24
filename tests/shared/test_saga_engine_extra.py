import pytest


@pytest.fixture(autouse=True)
def _pin_master_key(monkeypatch):
    """Saga files are encrypted with the session master key; other tests may
    clear/re-resolve the key cache mid-suite. Pin it so save/load always agree."""

    monkeypatch.setenv("MCP_MASTER_KEY", "test-secret-for-unit-tests-only")
    from features import secrets as _secrets

    _secrets._master_cache.clear()


import asyncio
from unittest.mock import AsyncMock
from shared.saga.engine import SagaEngine, SagaStep
from shared.saga.schema import SagaState, SagaStatus
from shared.saga.persistence import FileSagaStore
import tempfile
import shutil
from pathlib import Path


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d)


@pytest.fixture
def store(temp_dir):
    return FileSagaStore(temp_dir)


@pytest.fixture
def engine(store):
    return SagaEngine(store)


@pytest.mark.asyncio
async def test_saga_step_timeout_during_retry(engine):
    """Verify that timeout applies to each retry attempt."""
    state = SagaState(saga_id="test_timeout_retry", name="timeout_retry")

    calls = 0

    async def slow_step(ctx):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.5)
        return {"ok": True}

    steps = [SagaStep(name="s1", action=slow_step, timeout_seconds=0.1, retry_attempts=2, retry_backoff=0.01)]

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await engine.execute(state, steps)

    # Initial + 2 retries = 3 calls
    assert calls == 3
    assert state.status == SagaStatus.COMPENSATED


@pytest.mark.asyncio
async def test_saga_compensation_failure_isolation(engine):
    """Verify that if one compensation fails, others still run."""
    state = SagaState(saga_id="test_comp_fail", name="comp_fail")

    comp1_called = False
    comp2_called = False

    async def s1_comp(ctx):
        nonlocal comp1_called
        comp1_called = True

    async def s2_comp(ctx):
        nonlocal comp2_called
        comp2_called = True
        raise ValueError("comp2 failed")

    async def s3_action(ctx):
        raise RuntimeError("trigger compensation")

    steps = [
        SagaStep(name="s1", action=AsyncMock(return_value={}), compensation=s1_comp),
        SagaStep(name="s2", action=AsyncMock(return_value={}), compensation=s2_comp),
        SagaStep(name="s3", action=s3_action),
    ]

    with pytest.raises(RuntimeError, match="trigger compensation"):
        await engine.execute(state, steps)

    assert comp2_called is True
    assert comp1_called is True  # Should still be called despite s2_comp failure
    assert state.status == SagaStatus.COMPENSATED


@pytest.mark.asyncio
async def test_saga_context_persistence_between_steps(engine, store):
    """Verify context is saved to store after each successful step."""
    state = SagaState(saga_id="test_context_persist", name="context_persist")

    async def step1(ctx):
        return {"key1": "val1"}

    async def step2(ctx):
        # Verify persistence of step1 result in store
        loaded = store.load("test_context_persist")
        assert loaded.context.get("key1") == "val1"
        return {"key2": "val2"}

    steps = [SagaStep(name="s1", action=step1), SagaStep(name="s2", action=step2)]

    await engine.execute(state, steps)

    final_loaded = store.load("test_context_persist")
    assert final_loaded.context == {"key1": "val1", "key2": "val2"}
