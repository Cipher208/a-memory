"""Tests for envelope encryption — remaining unit tests."""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def master_key_env():
    os.environ["MCP_MASTER_KEY"] = "test-secret-for-unit-tests-only"
    from features import secrets

    secrets._master_cache.clear()
    yield
    os.environ.pop("MCP_MASTER_KEY", None)


def test_tampered_ciphertext_rejected():
    from features.secrets import decrypt_json, encrypt_json

    blob = encrypt_json({"x": 1})
    tampered = bytearray(blob)
    tampered[30] ^= 0x80
    with pytest.raises(Exception):
        decrypt_json(bytes(tampered))


def test_is_encrypted_blob(tmp_path: Path):
    from features.secrets import is_encrypted_blob

    plain = tmp_path / "plain.json"
    enc = tmp_path / "enc.json"
    plain.write_text('{"a": 1}')
    enc.write_bytes(b"\xab\xcd" * 30)
    assert not is_encrypted_blob(plain)
    assert is_encrypted_blob(enc)


@pytest.mark.parametrize(
    ("env_content", "expected_key"),
    [
        ("MCP_MASTER_KEY=from-dotenv-test", "from-dotenv-test"),
        ("# comment\n\nMCP_MASTER_KEY=real-value\n", "real-value"),
    ],
)
def test_dotenv_roundtrip(tmp_path, monkeypatch, env_content, expected_key):
    """_save_dotenv writes, _load_dotenv reads. Comments/blanks ignored."""
    from features.secrets import _load_dotenv, _save_dotenv

    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_MASTER_KEY", raising=False)

    if env_content.startswith("#"):
        # legacy repo-root .env — still read
        (tmp_path / ".env").write_text(env_content)
    else:
        _save_dotenv("MCP_MASTER_KEY", env_content.split("=", 1)[1])

    _load_dotenv()
    assert os.environ.get("MCP_MASTER_KEY") == expected_key


def test_dotenv_does_not_override_existing(monkeypatch):
    from features.secrets import _load_dotenv

    monkeypatch.setenv("MCP_MASTER_KEY", "already-set")
    _load_dotenv()
    assert os.environ.get("MCP_MASTER_KEY") == "already-set"


def test_master_key_derivation(monkeypatch):
    from features.secrets import _load_master_key, _master_cache

    monkeypatch.setenv("MCP_MASTER_KEY", "my-secret-seed-for-kdf")
    _master_cache.clear()
    key = _load_master_key()
    assert isinstance(key, bytes)
    assert len(key) == 32


def test_master_key_caches():
    from features.secrets import _get_master_key, _master_cache

    _master_cache.clear()
    key1 = _get_master_key()
    key2 = _get_master_key()
    assert key1 is key2
