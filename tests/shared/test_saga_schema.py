import pytest
from pydantic import ValidationError
from shared.saga.schema import SagaStatus, SagaStepState, SagaState
import time

def test_saga_status_enum():
    assert SagaStatus.PENDING == "pending"
    assert SagaStatus.STUCK == "stuck"

def test_saga_step_state_validation():
    step = SagaStepState(
        name="test_step",
        status=SagaStatus.PENDING,
        result={"foo": "bar"}
    )
    assert step.name == "test_step"
    assert step.status == SagaStatus.PENDING
    assert step.result == {"foo": "bar"}
    assert step.error is None

def test_saga_state_validation():
    saga_id = "abc12345"
    state = SagaState(
        saga_id=saga_id,
        name="test_saga",
        context={"user_id": 1},
        steps=[
            SagaStepState(name="step1", status=SagaStatus.COMPLETED, result={"ok": True})
        ],
        current_step_index=0,
        status=SagaStatus.RUNNING,
        started_at=time.time()
    )
    assert state.saga_id == saga_id
    assert len(state.steps) == 1
    assert state.status == SagaStatus.RUNNING

def test_saga_state_serialization():
    state = SagaState(
        saga_id="deadbeef",
        name="test",
        context={},
        steps=[],
        current_step_index=0,
        status=SagaStatus.PENDING,
        started_at=time.time()
    )
    json_data = state.model_dump_json()
    assert "deadbeef" in json_data
    
    restored = SagaState.model_validate_json(json_data)
    assert restored.saga_id == "deadbeef"

if __name__ == "__main__":
    test_saga_status_enum()
    test_saga_step_state_validation()
    test_saga_state_validation()
    test_saga_state_serialization()
    print("All tests passed!")
