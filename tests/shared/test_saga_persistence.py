import pytest
from shared.saga.schema import SagaState, SagaStatus
from shared.saga.persistence import FileSagaStore

@pytest.fixture
def temp_saga_store(tmp_path):
    return FileSagaStore(tmp_path)

def test_save_and_load(temp_saga_store):
    state = SagaState(
        saga_id="test_1",
        name="test_saga",
        status=SagaStatus.RUNNING,
        started_at=12345.67
    )
    temp_saga_store.save(state)

    loaded = temp_saga_store.load("test_1")
    assert loaded is not None
    assert loaded.saga_id == "test_1"
    assert loaded.name == "test_saga"
    assert loaded.status == SagaStatus.RUNNING

def test_delete(temp_saga_store):
    state = SagaState(saga_id="to_delete", name="delete_me")
    temp_saga_store.save(state)
    assert temp_saga_store.load("to_delete") is not None

    temp_saga_store.delete("to_delete")
    assert temp_saga_store.load("to_delete") is None

def test_list_all(temp_saga_store):
    sagas = [
        SagaState(saga_id=f"saga_{i}", name=f"name_{i}")
        for i in range(3)
    ]
    for s in sagas:
        temp_saga_store.save(s)

    all_sagas = temp_saga_store.list_all()
    assert len(all_sagas) == 3
    ids = {s.saga_id for s in all_sagas}
    assert ids == {"saga_0", "saga_1", "saga_2"}

def test_load_nonexistent(temp_saga_store):
    assert temp_saga_store.load("ghost") is None
