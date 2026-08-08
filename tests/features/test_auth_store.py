import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from features.auth.models import AuthTokenModel
from features.auth.store import EncryptedStore


class MockModel(BaseModel):
    name: str
    value: int


@pytest.fixture
def temp_store(tmp_path):
    file_path = tmp_path / "test_store.json"
    return EncryptedStore(file_path, MockModel)


def test_encryption_decryption_cycle(temp_store):
    data = {"name": "test", "value": 123}
    temp_store.save(data)
    
    assert temp_store.file_path.exists()
    
    loaded = temp_store.load()
    assert loaded == data


def test_atomic_write(temp_store, tmp_path):
    data = {"name": "atomic", "value": 1}
    temp_store.save(data)
    
    # Check permissions (0o600)
    mode = os.stat(temp_store.file_path).st_mode & 0o777
    assert mode == 0o600


def test_legacy_json_rotation(tmp_path):
    file_path = tmp_path / "legacy.json"
    legacy_data = {"name": "legacy", "value": 99}
    
    # Write plain JSON
    with file_path.open("w") as f:
        json.dump(legacy_data, f)
        
    store = EncryptedStore(file_path, MockModel)
    
    # Load should work and rotate
    loaded = store.load()
    assert loaded == legacy_data
    
    # Verify it is now encrypted (not valid JSON or not decodable as utf-8)
    with file_path.open("rb") as f:
        content = f.read()
        is_json = False
        try:
            json.loads(content.decode("utf-8"))
            is_json = True
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        assert not is_json
            
    # Load again should still work (using decryption)
    assert store.load() == legacy_data
