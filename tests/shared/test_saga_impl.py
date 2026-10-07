import pytest


@pytest.fixture(autouse=True)
def _pin_master_key(monkeypatch):
    """Saga files are encrypted with the session master key; other tests may
    clear/re-resolve the key cache mid-suite. Pin it so save/load always agree."""

    monkeypatch.setenv("MCP_MASTER_KEY", "test-secret-for-unit-tests-only")
    from features import secrets as _secrets

    _secrets._master_cache.clear()


from unittest.mock import AsyncMock
from pathlib import Path
import tempfile

from shared.saga.engine import SagaEngine, SagaStep
from shared.saga.schema import SagaState, SagaStatus
from shared.saga.persistence import FileSagaStore
from shared.saga.impl.backup import create_backup_saga
from shared.saga.impl.consolidation import create_consolidation_saga
from shared.constants import DB_NAME


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def store(temp_dir):
    return FileSagaStore(temp_dir)


@pytest.fixture
def engine(store):
    return SagaEngine(store)


@pytest.mark.asyncio
async def test_backup_saga_success(engine, temp_dir, monkeypatch):
    # The saga resolves its base dir from MCP_MEMORY_DATA_DIR, which the suite
    # now points at its own session dir (see tests/conftest.py), so patching
    # `pathlib.Path.home` no longer decides anything: the variable wins. The
    # test has to say where it wants its database instead of relying on the
    # variable being unset.
    base = temp_dir / ".mcp-ariel-memory"
    base.mkdir(parents=True)
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(base))
    (base / DB_NAME).write_text("dummy database content")

    steps = create_backup_saga()
    state = SagaState(saga_id="backup_test", name="backup", context={})

    # Execute
    result = await engine.execute(state, steps)

    # Verify
    assert "backup_path" in result
    backup_path = Path(result["backup_path"])
    assert backup_path.exists()
    assert (backup_path / DB_NAME).exists()
    assert state.status == SagaStatus.COMPLETED


@pytest.mark.asyncio
async def test_backup_saga_compensation(engine, temp_dir, monkeypatch):
    # Setup mock environment where verification fails
    base = temp_dir / ".mcp-ariel-memory"
    base.mkdir(parents=True)
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(base))
    (base / DB_NAME).write_text("dummy database content")

    steps = create_backup_saga()
    # Force failure in second step
    steps[1].action = AsyncMock(side_effect=ValueError("verify failed"))

    state = SagaState(saga_id="backup_fail", name="backup", context={})

    # Execute
    with pytest.raises(ValueError, match="verify failed"):
        await engine.execute(state, steps)

    # Verify compensation (backup dir removed)
    assert state.status == SagaStatus.COMPENSATED
    # The backup has to have been made for its removal to mean anything; before
    # this test named its own data dir the source was missing, the first step
    # reported `skipped_no_source`, and this assertion never ran.
    backup_path_str = state.context.get("backup_path")
    assert backup_path_str, "the backup step must have produced a directory to compensate"
    assert not Path(backup_path_str).exists()


@pytest.mark.asyncio
async def test_consolidation_saga_success(engine):
    # Mock Memory Manager
    mm = AsyncMock()
    mm.search.return_value = [
        {"key": "k1", "value": "v1", "importance": 0.9},
        {"key": "k2", "value": "v2", "importance": 0.5},
        {"key": "k3", "value": "v3", "importance": 0.8},
    ]

    user_id = "user123"
    steps = create_consolidation_saga(user_id, mm)
    state = SagaState(saga_id="cons_test", name="consolidation", context={"user_id": user_id, "_mm": mm})

    # Execute
    result = await engine.execute(state, steps)

    # Verify
    assert result["gathered_count"] == 3
    assert result["distilled_count"] == 2
    assert result["promoted_count"] == 2
    assert state.status == SagaStatus.COMPLETED

    # Check MM calls
    assert mm.save.call_count == 2
    mm.save.assert_any_call(user_id=user_id, key="k1", value="v1", importance=0.9, memory_kind="fact", source="consolidation")


@pytest.mark.asyncio
async def test_consolidation_saga_rollback(engine):
    mm = AsyncMock()
    mm.search.return_value = [
        {"key": "k1", "value": "v1", "importance": 0.9},
    ]
    # Force failure after promotion (e.g. step not exist or engine error)

    user_id = "user123"
    steps = create_consolidation_saga(user_id, mm)

    # Add a dummy failing step at the end to trigger compensation
    steps.append(SagaStep(name="fail", action=AsyncMock(side_effect=RuntimeError("trigger rollback"))))

    state = SagaState(saga_id="cons_fail", name="consolidation", context={"user_id": user_id, "_mm": mm})

    # Execute
    with pytest.raises(RuntimeError, match="trigger rollback"):
        await engine.execute(state, steps)

    # Verify rollback
    assert state.status == SagaStatus.COMPENSATED
    mm.delete.assert_called_once_with(user_id, "k1")
