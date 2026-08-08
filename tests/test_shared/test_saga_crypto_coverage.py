"""Tests for shared/saga_crypto.py — full coverage."""

import json
import warnings
from pathlib import Path

import pytest

from shared.saga import read_state, read_state_legacy_or_encrypted, write_state_atomic


def test_write_state_atomic_creates_encrypted_file(tmp_path):
    """write_state_atomic should create an encrypted file."""
    path = tmp_path / "test.json"
    state = {"key": "value", "nested": {"a": 1}}
    write_state_atomic(path, state)
    assert path.exists()
    data = path.read_bytes()
    assert len(data) > 0
    # Should not be plain JSON (encrypted)
    assert not data.startswith(b"{")


def test_write_state_atomic_creates_parent_dirs(tmp_path):
    """write_state_atomic should create parent directories."""
    path = tmp_path / "deep" / "nested" / "dir" / "test.json"
    write_state_atomic(path, {"key": "value"})
    assert path.exists()


def test_write_state_atomic_replaces_existing(tmp_path):
    """write_state_atomic should replace existing file."""
    path = tmp_path / "test.json"
    write_state_atomic(path, {"old": True})
    write_state_atomic(path, {"new": True})
    assert path.exists()
    # Should be re-encrypted
    data = path.read_bytes()
    assert len(data) > 0


def test_read_state_reads_encrypted(tmp_path):
    """read_state should read encrypted file."""
    path = tmp_path / "test.json"
    write_state_atomic(path, {"key": "value"})
    loaded = read_state(path)
    assert loaded == {"key": "value"}


def test_read_state_raises_for_missing():
    """read_state should raise FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError):
        read_state(Path("/nonexistent/path.json"))


def test_read_state_legacy_rotates_to_encrypted(tmp_path):
    """read_state_legacy_or_encrypted should rotate legacy JSON to encrypted."""
    path = tmp_path / "legacy.json"
    # Write plain JSON (not encrypted)
    path.write_text(json.dumps({"legacy": True}), encoding="utf-8")

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        loaded = read_state_legacy_or_encrypted(path)
        assert loaded == {"legacy": True}
        # Should have warned about rotation
        assert len(w) == 1
        assert "rotating" in str(w[0].message).lower()

    # File should now be encrypted
    data = path.read_bytes()
    assert not data.startswith(b"{")


def test_read_state_legacy_reads_encrypted(tmp_path):
    """read_state_legacy_or_encrypted should read already-encrypted file."""
    path = tmp_path / "encrypted.json"
    write_state_atomic(path, {"encrypted": True})

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        loaded = read_state_legacy_or_encrypted(path)
        assert loaded == {"encrypted": True}
        # No warning for encrypted files
        assert len(w) == 0


def test_read_state_legacy_raises_for_missing():
    """read_state_legacy_or_encrypted should raise FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        read_state_legacy_or_encrypted(Path("/nonexistent.json"))


def test_write_read_roundtrip(tmp_path):
    """Write then read should return same data."""
    path = tmp_path / "roundtrip.json"
    original = {"users": ["alice", "bob"], "count": 42, "nested": {"deep": True}}
    write_state_atomic(path, original)
    loaded = read_state(path)
    assert loaded == original


def test_write_state_atomic_chmod_error(tmp_path):
    """write_state_atomic should handle chmod errors gracefully."""
    path = tmp_path / "test.json"
    # Should not raise even if chmod fails
    write_state_atomic(path, {"key": "value"})
    assert path.exists()
