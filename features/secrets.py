"""Envelope-encrypt JSON secrets — backward-compat shim.

Implementation moved to shared/master_key.py so shared.saga no longer
imports features (broke the features <-> shared import cycles).
"""

from shared.master_key import (  # noqa: F401
    _get_master_key,
    _load_dotenv,
    _load_master_key,
    _master_cache,
    _save_dotenv,
    decrypt_json,
    encrypt_json,
    is_encrypted_blob,
)

__all__ = [
    "decrypt_json",
    "encrypt_json",
    "is_encrypted_blob",
]
