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
    assert crypto.is_encrypted_blob(blob, key)
    assert crypto.decrypt_json(blob, key) == payload


def test_detection_survives_a_nonce_that_looks_like_json(key):
    """Regression: detection must not depend on the nonce's first byte.

    The envelope is nonce(24) || ciphertext and the nonce is random, so its
    first byte is `{` in about 1 draw out of 256. The old first-byte test
    pronounced such real ciphertext "plain JSON" — measured 69 of 4096 draws
    (1.68%) — which made a readable secret look missing. This test waits for
    that exact draw instead of avoiding it: the loop needs ~256 iterations, and
    (255/256)**16384 ≈ 0, so it is deterministic in practice.
    """
    payload = {"secret": "value"}
    blob = b""
    for _ in range(16384):
        candidate = crypto.encrypt_json(payload, key)
        if candidate[:1] == b"{":
            blob = candidate
            break
    assert blob, "could not obtain a blob starting with '{' — test would be vacuous"
    assert blob[:1] == b"{"  # the very draw that used to break detection
    assert crypto.is_encrypted_blob(blob, key)
    assert crypto.decrypt_json(blob, key) == payload


def test_detection_rejects_anything_that_is_not_a_readable_envelope(key):
    assert not crypto.is_encrypted_blob(b'{"plain": true}', key)  # plain JSON
    assert not crypto.is_encrypted_blob(b"", key)  # empty
    assert not crypto.is_encrypted_blob(b"\x00" * 10, key)  # shorter than nonce+MAC
    assert not crypto.is_encrypted_blob(b"\xab\xcd" * 30, key)  # filler, not an envelope
    foreign = crypto.encrypt_json({"a": 1}, os.urandom(32))
    assert not crypto.is_encrypted_blob(foreign, key)  # encrypted, but under another key
    tampered = bytearray(crypto.encrypt_json({"a": 1}, key))
    tampered[-1] ^= 0xFF
    assert not crypto.is_encrypted_blob(bytes(tampered), key)  # MAC no longer matches


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
