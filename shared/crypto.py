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


def encrypt_json(data: dict | list, master_key: bytes) -> bytes:
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


def is_encrypted_blob(blob_head: bytes) -> bool:
    """Check if data starts like an encrypted blob (heuristic).
    
    JSON starts with { or [.
    """
    if not blob_head:
        return False
    head = blob_head[:1]
    return head not in (b"{", b"[", b" ", b"\n")
