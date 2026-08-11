from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

from features.auth.models import APIKeyModel
from features.auth.store import EncryptedStore


class APIKeyAuth:
    def __init__(self, keys_file: Path | None = None) -> None:
        if keys_file is None:
            # Default location if not provided
            keys_file = Path("data/auth/keys.enc")

        self.store = EncryptedStore(keys_file, APIKeyModel)
        self._keys: dict[str, dict[str, Any]] = self.store.load()

    def create_key(self, user_id: str, label: str) -> str:
        """Generate ak_... key, save state."""
        key = f"ak_{secrets.token_hex(24)}"

        model = APIKeyModel(user_id=user_id, label=label, created_at=time.time(), enabled=True)

        self._keys[key] = model.model_dump()
        self.store.save(self._keys)
        return key

    def verify(self, key: str) -> dict[str, Any] | None:
        """
        Check key validity and enabled status.
        Update last_used timestamp and save() on success.
        Return {"user_id": ..., "label": ...}.
        """
        data = self._keys.get(key)
        if not data:
            return None

        model = APIKeyModel(**data)
        if not model.enabled:
            return None

        # Update last_used
        model.last_used = time.time()
        self._keys[key] = model.model_dump()
        self.store.save(self._keys)

        return {"user_id": model.user_id, "label": model.label}

    def revoke(self, key: str) -> bool:
        """Set enabled=False, save."""
        data = self._keys.get(key)
        if not data:
            return False

        model = APIKeyModel(**data)
        model.enabled = False
        self._keys[key] = model.model_dump()
        self.store.save(self._keys)
        return True

    def delete_key(self, key: str) -> bool:
        """Remove key from store."""
        if key in self._keys:
            del self._keys[key]
            self.store.save(self._keys)
            return True
        return False

    def list_keys(self) -> list[dict[str, Any]]:
        """Return masked keys with metadata."""
        result = []
        for key, data in self._keys.items():
            model = APIKeyModel(**data)
            masked_key = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"

            entry = model.model_dump()
            entry["key"] = masked_key
            result.append(entry)
        return result
