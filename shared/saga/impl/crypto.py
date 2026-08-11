"""Saga state encryption — atomic writes with envelope encryption.

Wraps features.secrets for saga-specific state persistence.
"""

from __future__ import annotations

import contextlib
import json
import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from features.secrets import decrypt_json, encrypt_json
from shared.crypto import is_encrypted_blob as _is_crypto_encrypted_blob


def is_encrypted_blob(path: Path) -> bool:
    """Check if file is encrypted (not plain JSON)."""
    if not path.exists():
        return False
    # noqa: SKY-D325
    with path.open("rb") as f:
        head = f.read(1)
    return _is_crypto_encrypted_blob(head)

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
    with tmp.open("wb") as f:
        # noqa: SKY-D324
        f.write(blob)
    with contextlib.suppress(OSError, PermissionError):
        os.chmod(tmp, 0o600)
    # noqa: SKY-D324
    tmp.replace(path)
    with contextlib.suppress(OSError, PermissionError):
        os.chmod(path, 0o600)


def read_state(path: Path) -> dict:
    """Read encrypted state file."""
    if not path.exists():
        raise FileNotFoundError(path)
    # noqa: SKY-D325
    with path.open("rb") as f:
        blob = f.read()
    return decrypt_json(blob)


def read_state_legacy_or_encrypted(path: Path) -> dict:
    """Backward-compat: reads legacy plain JSON or encrypted, rotates legacy to encrypted."""
    if not path.exists():
        raise FileNotFoundError(path)
    # noqa: SKY-D325
    with path.open("rb") as f:
        blob = f.read()
    if is_encrypted_blob(path):
        return decrypt_json(blob)
    warnings.warn(f"{path} is plain JSON; rotating to encrypted", DeprecationWarning, stacklevel=2)
    legacy = json.loads(blob.decode("utf-8"))
    write_state_atomic(path, legacy)
    return legacy
