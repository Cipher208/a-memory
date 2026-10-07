"""Core encryption logic for envelope-encrypting JSON secrets and saga state.

Master key is provided from high-level features/secrets.py.
This module is self-contained and only depends on basic Python libs and libsodium (pynacl).
"""

from __future__ import annotations

import json
from typing import Any

try:
    from nacl.secret import SecretBox
    from nacl.utils import random as nacl_random

    _HAS_NACL = True
except ImportError:
    _HAS_NACL = False

# Constants shared between features/secrets and shared/crypto
_MASTER_KEY_LEN = 32
_NONCE_SIZE = 24
_MAC_SIZE = 16


def encrypt_json(data: dict[str, Any] | list[Any], master_key: bytes) -> bytes:
    """Encrypt JSON data with given master_key. Returns nonce(24) || ciphertext."""
    if not _HAS_NACL:
        raise ImportError("pynacl is required for encryption. Install with: pip install pynacl")

    box = SecretBox(master_key)
    nonce = nacl_random(SecretBox.NONCE_SIZE)
    plaintext = json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    return nonce + box.encrypt(plaintext, nonce).ciphertext


def decrypt_json(blob: bytes, master_key: bytes) -> Any:
    """Decrypt blob back to JSON using given master_key."""
    if not _HAS_NACL:
        raise ImportError("pynacl is required for encryption. Install with: pip install pynacl")

    if len(blob) < _NONCE_SIZE + _MAC_SIZE:
        raise ValueError("blob too short for valid SecretBox message")

    nonce, ct = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:]
    box = SecretBox(master_key)
    return json.loads(box.decrypt(ct, nonce).decode("utf-8"))


def is_encrypted_blob(blob: bytes, master_key: bytes) -> bool:
    """Return True when `blob` is a readable SecretBox envelope.

    Decided by attempting decryption, never by inspecting a byte. The envelope
    is `nonce(24) || ciphertext`, so its first byte is the first byte of a
    random nonce — `{` in roughly one draw out of 256. The previous first-byte
    test consequently reported real ciphertext as "plain JSON" in 4 cases out
    of 256 (measured: 69 of 4096 = 1.68%), which is the wrong way for a
    predicate to fail: it turns a readable secret into a "missing" one.

    Pass the whole blob. A truncated one cannot authenticate and reports False.
    """
    try:
        decrypt_json(blob, master_key)
    except Exception:
        # Too short, bad MAC, wrong key, or a plaintext that is not JSON: in
        # every one of those cases this is not a readable envelope.
        return False
    return True
