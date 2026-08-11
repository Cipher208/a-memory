from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, cast, TYPE_CHECKING

from features.secrets import decrypt_json, encrypt_json

if TYPE_CHECKING:
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

    def load(self) -> dict[str, Any]:
        """Load data, decrypting it. Supports rotation from legacy JSON."""
        if not self.file_path.exists():
            return {}

        try:
            with self.file_path.open("rb") as f:
                blob = f.read()
        except Exception as e:
            logger.warning("Failed to read %s: %s", self.file_path, e)
            return {}

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
            return {}
