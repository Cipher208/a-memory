import pytest
import asyncio
from unittest.mock import MagicMock
from pathlib import Path
import tempfile
import shutil

from shared.saga.engine import SagaEngine, SagaStep
from shared.saga.schema import SagaState, SagaStatus
from shared.saga.persistence import FileSagaStore


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
async def test_saga_success(engine, store):
    # Setup
    state = SagaState(saga_id="test1", name="success_saga")

    async def step1(ctx):
        return {"a": 1}

    async def step2(ctx):
        return {"b": ctx["a"] + 1}

    steps = [SagaStep(name="s1", action=step1), SagaStep(name="s2", action=step2)]

    # Execute
    result = await engine.execute(state, steps)

    # Verify
    assert result == {"a": 1, "b": 2}
    assert state.status == SagaStatus.COMPLETED
    assert len(state.steps) == 2
    assert state.steps[0].status == SagaStatus.COMPLETED
    assert state.steps[1].status == SagaStatus.COMPLETED

    # Verify persistence
    loaded = store.load("test1")
    assert loaded.status == SagaStatus.COMPLETED
    assert loaded.context == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_saga_idempotency(engine, store):
    # Setup state with first step already completed
    state = SagaState(
        saga_id="test2",
        name="idemp_saga",
        context={"a": 10},
        steps=[{"name": "s1", "status": SagaStatus.COMPLETED, "result": {"a": 10}}, {"name": "s2", "status": SagaStatus.PENDING}],
    )
    store.save(state)

    s1_mock = MagicMock()

    async def s2_action(ctx):
        return {"b": ctx["a"] + 5}

    steps = [SagaStep(name="s1", action=s1_mock), SagaStep(name="s2", action=s2_action)]

    # Execute
    result = await engine.execute(state, steps)

    # Verify s1 was skipped
    s1_mock.assert_not_called()
    assert result == {"a": 10, "b": 15}
    assert state.steps[1].status == SagaStatus.COMPLETED


@pytest.mark.asyncio
async def test_saga_retry(engine):
    state = SagaState(saga_id="test3", name="retry_saga")

    calls = 0

    async def failing_step(ctx):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ValueError("try again")
        return {"ok": True}

    steps = [SagaStep(name="s1", action=failing_step, retry_attempts=3, retry_backoff=0.01)]

    result = await engine.execute(state, steps)
    assert result == {"ok": True}
    assert calls == 3


@pytest.mark.asyncio
async def test_saga_compensation(engine):
    state = SagaState(saga_id="test4", name="comp_saga")

    comp_called = False

    async def s1_action(ctx):
        return {"data": "step1"}

    async def s1_comp(ctx):
        nonlocal comp_called
        comp_called = True

    async def s2_action(ctx):
        raise ValueError("boom")

    steps = [SagaStep(name="s1", action=s1_action, compensation=s1_comp), SagaStep(name="s2", action=s2_action)]

    with pytest.raises(ValueError, match="boom"):
        await engine.execute(state, steps)

    assert state.status == SagaStatus.COMPENSATED
    assert comp_called is True
    assert state.steps[0].status == SagaStatus.COMPLETED
    assert state.steps[1].status == SagaStatus.FAILED


@pytest.mark.asyncio
async def test_saga_timeout(engine):
    state = SagaState(saga_id="test_timeout", name="timeout_saga")

    async def slow_step(ctx):
        await asyncio.sleep(0.5)
        return {"ok": True}

    steps = [SagaStep(name="s1", action=slow_step, timeout_seconds=0.1)]

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await engine.execute(state, steps)

    assert state.status == SagaStatus.COMPENSATED
    assert state.steps[0].status == SagaStatus.FAILED
