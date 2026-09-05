"""T17 (аудит 05.09): smoke-тесты крипты — shared/crypto.py не имел ни одного теста.

Round-trip encrypt/decrypt + tamper detection + is_encrypted_blob контракт.
"""

from __future__ import annotations

import os

import pytest

crypto = pytest.importorskip("shared.crypto")


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


def test_roundtrip_dict(key):
    payload = {"k": "v", "n": 42, "nested": {"a": [1, 2, 3]}}
    blob = crypto.encrypt_json(payload, key)
    assert blob != b"" and crypto.is_encrypted_blob(blob[:8])
    assert crypto.decrypt_json(blob, key) == payload


def test_roundtrip_list_and_unicode(key):
    payload = ["строка", "второй", 3]
    blob = crypto.encrypt_json(payload, key)
    assert crypto.decrypt_json(blob, key) == payload


def test_wrong_key_raises(key):
    blob = crypto.encrypt_json({"secret": True}, key)
    with pytest.raises(Exception):
        crypto.decrypt_json(blob, os.urandom(32))


def test_tampered_blob_raises(key):
    blob = bytearray(crypto.encrypt_json({"x": 1}, key))
    blob[-1] ^= 0xFF  # флип бита шифротекста → MAC не сходится
    with pytest.raises(Exception):
        crypto.decrypt_json(bytes(blob), key)


def test_too_short_blob_raises(key):
    with pytest.raises(ValueError, match="too short"):
        crypto.decrypt_json(b"\x00" * 10, key)
