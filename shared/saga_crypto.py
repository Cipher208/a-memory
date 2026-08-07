"""Saga state encryption — atomic writes with envelope encryption.

Wraps features.secrets for saga-specific state persistence.
"""

from __future__ import annotations

import contextlib
import json
import os
import warnings
from typing import TYPE_CHECKING

from features.secrets import decrypt_json, encrypt_json, is_encrypted_blob

if TYPE_CHECKING:
    from pathlib import Path


def write_state_atomic(path: Path, state: dict) -> None:
    """Atomic write with encryption.

    Format: nonce(24) || ciphertext (libsodium secretbox).
    Writes to tmp then renames for crash safety.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = encrypt_json(state)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
    with contextlib.suppress(OSError, PermissionError):
        os.chmod(tmp, 0o600)
    tmp.replace(path)
    with contextlib.suppress(OSError, PermissionError):
        os.chmod(path, 0o600)


def read_state(path: Path) -> dict:
    """Read encrypted state file."""
    if not path.exists():
        raise FileNotFoundError(path)
    with open(path, "rb") as f:
        blob = f.read()
    return decrypt_json(blob)


def read_state_legacy_or_encrypted(path: Path) -> dict:
    """Backward-compat: reads legacy plain JSON or encrypted, rotates legacy to encrypted."""
    if not path.exists():
        raise FileNotFoundError(path)
    with open(path, "rb") as f:
        blob = f.read()
    if is_encrypted_blob(path):
        return decrypt_json(blob)
    warnings.warn(f"{path} is plain JSON; rotating to encrypted", DeprecationWarning, stacklevel=2)
    legacy = json.loads(blob.decode("utf-8"))
    write_state_atomic(path, legacy)
    return legacy
