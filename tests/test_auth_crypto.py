"""End-to-end tests for auth with encryption."""

import json
import os

import pytest


@pytest.fixture(autouse=True)
def restore_master_key():
    """Restore MCP_MASTER_KEY after each test to prevent cross-test contamination."""
    original = os.environ.get("MCP_MASTER_KEY")
    yield
    if original is not None:
        os.environ["MCP_MASTER_KEY"] = original
    else:
        os.environ.pop("MCP_MASTER_KEY", None)
    from features import secrets

    secrets._master_cache.clear()


def test_legacy_plain_json_gets_rotated(tmp_path):
    """If file is plain JSON, it should be auto-encrypted on load."""
    keys_file = tmp_path / "api_keys.json"
    keys_file.write_text(json.dumps({"ak_old": {"user_id": "u1", "label": "old", "enabled": True, "created_at": 1.0}}))
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=str(keys_file))
    keys = auth.list_keys()
    assert len(keys) == 1
    assert keys[0]["user_id"] == "u1"

    # Verify data survived the rotation — read with fresh instance
    auth2 = APIKeyAuth(keys_file=str(keys_file))
    keys2 = auth2.list_keys()
    assert len(keys2) == 1
    assert keys2[0]["user_id"] == "u1"


def test_create_then_verify_round_trip(tmp_path):
    keys_file = tmp_path / "api_keys.json"
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=str(keys_file))
    key = auth.create_key("alice", "test-key")
    out = auth.verify(key)
    assert out == {"user_id": "alice", "label": "test-key"}


def test_wrong_master_key_fails(tmp_path):
    keys_file = tmp_path / "api_keys.json"
    from features.auth import APIKeyAuth

    auth = APIKeyAuth(keys_file=str(keys_file))
    auth.create_key("bob", "test-label")

    # Change master key
    os.environ["MCP_MASTER_KEY"] = "different-secret"
    from features import secrets

    secrets._master_cache.clear()

    # Should fail to decrypt (returns empty dict, not exception)
    auth2 = APIKeyAuth(keys_file=str(keys_file))
    assert auth2.list_keys() == []  # cannot read old keys


def test_bearer_token_encrypted(tmp_path):
    token_file = tmp_path / "bearer_token.json"
    from features.auth import BearerAuth

    auth = BearerAuth(token_file=str(token_file))
    token = auth.get_token()
    assert token.startswith("mt_")

    # Verify round-trip works
    assert auth.verify(f"Bearer {token}") is True
    assert auth.verify("Bearer invalid") is False

    # Rotation produces a new token
    old_token = token
    new_token = auth.rotate()
    assert new_token.startswith("mt_")
    assert new_token != old_token
    assert auth.verify(f"Bearer {new_token}") is True
    assert auth.verify(f"Bearer {old_token}") is False
