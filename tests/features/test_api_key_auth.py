from __future__ import annotations

import time

import pytest

from features.auth.api_key import APIKeyAuth


@pytest.fixture
def keys_file(tmp_path):
    return tmp_path / "keys.enc"


@pytest.fixture
def auth(keys_file):
    return APIKeyAuth(keys_file)


def test_create_key(auth):
    key = auth.create_key(user_id="user_1", label="test_key")
    assert key.startswith("ak_")
    assert len(key) > 10

    keys = auth.list_keys()
    assert len(keys) == 1
    assert keys[0]["user_id"] == "user_1"
    assert keys[0]["label"] == "test_key"
    assert "key" in keys[0]
    # Mask is prefix...suffix (6 chars + ... + 4 chars)
    assert "..." in keys[0]["key"]
    assert len(keys[0]["key"]) == 13  # 6 + 3 + 4


def test_verify_success(auth):
    key = auth.create_key(user_id="user_2", label="active_key")

    # Wait a bit to ensure last_used changes (if precision allows)
    time.sleep(0.01)

    result = auth.verify(key)
    assert result is not None
    assert result["user_id"] == "user_2"
    assert result["label"] == "active_key"

    # Verify last_used is updated
    keys = auth.list_keys()
    assert keys[0]["last_used"] is not None
    assert keys[0]["last_used"] > 0


def test_verify_revoked(auth):
    key = auth.create_key(user_id="user_3", label="revoked_key")
    auth.revoke(key)

    result = auth.verify(key)
    assert result is None


def test_delete_key(auth):
    key = auth.create_key(user_id="user_4", label="to_delete")
    assert len(auth.list_keys()) == 1

    auth.delete_key(key)
    assert len(auth.list_keys()) == 0


def test_persistence(keys_file):
    auth1 = APIKeyAuth(keys_file)
    key = auth1.create_key(user_id="user_5", label="persistent")

    auth2 = APIKeyAuth(keys_file)
    result = auth2.verify(key)
    assert result is not None
    assert result["user_id"] == "user_5"
