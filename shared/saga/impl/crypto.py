"""Saga state encryption — atomic writes with envelope encryption.

Wraps features.secrets for saga-specific state persistence.
"""

from __future__ import annotations

import contextlib
import json
import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

from features.secrets import decrypt_json, encrypt_json
from shared.crypto import is_encrypted_blob as _is_crypto_encrypted_blob

__all__ = [
    "decrypt_json",
    "encrypt_json",
    "is_encrypted_blob",
    "read_state",
    "read_state_legacy_or_encrypted",
    "write_state_atomic",
]


def is_encrypted_blob(path: Path) -> bool:
    """Check if file is encrypted (not plain JSON)."""
    if not path.exists():
        return False
    with path.open("rb") as f:
        head = f.read(1)
    return bool(_is_crypto_encrypted_blob(head))


if TYPE_CHECKING:
    from pathlib import Path


def write_state_atomic(path: Path, state: dict[str, Any]) -> None:
    """Atomic write with encryption.

    Format: nonce(24) || ciphertext (libsodium secretbox).
    Writes to tmp then renames for crash safety.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = encrypt_json(state)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(blob)
    with contextlib.suppress(OSError, PermissionError):
        os.chmod(tmp, 0o600)
    tmp.replace(path)
    with contextlib.suppress(OSError, PermissionError):
        os.chmod(path, 0o600)


def read_state(path: Path) -> dict[str, Any]:
    """Read encrypted state file."""
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("rb") as f:
        blob = f.read()
    res: Any = decrypt_json(blob)
    return dict(res) if isinstance(res, dict) else {}


def read_state_legacy_or_encrypted(path: Path) -> dict[str, Any]:
    """Backward-compat: reads legacy plain JSON or encrypted, rotates legacy to encrypted."""
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("rb") as f:
        blob = f.read()
    if is_encrypted_blob(path):
        res: Any = decrypt_json(blob)
        return dict(res) if isinstance(res, dict) else {}
    warnings.warn(f"{path} is plain JSON; rotating to encrypted", DeprecationWarning, stacklevel=2)
    legacy: Any = json.loads(blob.decode("utf-8"))
    state: dict[str, Any] = dict(legacy) if isinstance(legacy, dict) else {}
    write_state_atomic(path, state)
    return state
