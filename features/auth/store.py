from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, cast, TYPE_CHECKING

from features.secrets import decrypt_json, encrypt_json

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel
logger = logging.getLogger(__name__)


class EncryptedStore:
    def __init__(self, file_path: Path, model_class: type[BaseModel]):
        self.file_path = Path(file_path)
        self.model_class = model_class
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, data: dict[str, Any]) -> None:
        """Encrypt and save data atomically with restricted permissions."""
        # Validation if needed, though prompt says save(data: dict)
        # We can validate via model_class if it's a single object or a dict of objects.
        # But for now, just encrypt what we got as per instructions.

        ciphertext = encrypt_json(data)
        tmp_file = self.file_path.with_suffix(".tmp")

        with tmp_file.open("wb") as f:
            f.write(ciphertext)

        with contextlib.suppress(OSError):
            os.chmod(tmp_file, 0o600)

        tmp_file.replace(self.file_path)

        with contextlib.suppress(OSError):
            os.chmod(self.file_path, 0o600)

    def _load(self) -> dict[str, Any] | None:
        """Load data; None means an existing file could not be read or decoded."""
        if not self.file_path.exists():
            return {}

        try:
            with self.file_path.open("rb") as f:
                blob = f.read()
        except Exception as e:
            logger.warning("Failed to read %s: %s", self.file_path, e)
            return None

        # 1. Try decrypt
        with contextlib.suppress(Exception):
            return cast("dict[str, Any]", decrypt_json(blob))

        # 2. Try legacy JSON
        try:
            legacy_data = cast("dict[str, Any]", json.loads(blob.decode("utf-8")))
            # Rotate immediately
            self.save(legacy_data)
            logger.info("Rotated legacy JSON in %s to encrypted format", self.file_path)
            return legacy_data
        except Exception:
            logger.exception("Failed to load %s as encrypted or legacy JSON", self.file_path)
            return None

    def load(self) -> dict[str, Any]:
        """Load data, decrypting it. Supports rotation from legacy JSON."""
        return self._load() or {}

    def update(self, mutator: Callable[[dict[str, Any]], object]) -> dict[str, Any] | None:
        """Read-modify-write the current on-disk state under an exclusive lock.

        The mutator receives a fresh copy of the current state and mutates it in
        place. Returns the saved state, or None when an existing file could not
        be loaded — nothing is written in that case, so a failed read never
        replaces the store with an empty dict.

        fcntl is POSIX-only and is imported locally so Windows keeps importing
        this module. Without it the lock is skipped: the fresh read right before
        the save narrows the race window but does not close it.
        """
        fcntl_module: Any = None
        with contextlib.suppress(ImportError):
            import fcntl as fcntl_module

        lock_path = self.file_path.with_name(self.file_path.name + ".lock")
        with lock_path.open("a+b") as lock_file:
            if fcntl_module is not None:
                fcntl_module.flock(lock_file.fileno(), fcntl_module.LOCK_EX)
            try:
                data = self._load()
                if data is None:
                    return None
                mutator(data)
                self.save(data)
                return data
            finally:
                if fcntl_module is not None:
                    fcntl_module.flock(lock_file.fileno(), fcntl_module.LOCK_UN)
