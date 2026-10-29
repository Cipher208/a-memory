from __future__ import annotations

import contextlib
import secrets
import time
from pathlib import Path
from typing import Any

from features.auth.models import APIKeyModel
from features.auth.store import EncryptedStore, StoreUnreadable


class APIKeyAuth:
    def __init__(self, keys_file: Path | None = None) -> None:
        if keys_file is None:
            # Default location if not provided
            keys_file = Path("data/auth/keys.enc")

        self.store = EncryptedStore(keys_file, APIKeyModel)
        self._keys: dict[str, dict[str, Any]] = {}
        # Construction stays tolerant: the module-level singleton is built at import
        # time, and an unreadable file must not take the server down. Every
        # operation below re-reads and raises, so the tolerance stops here.
        with contextlib.suppress(StoreUnreadable):
            self._keys = self.store.load_strict()

    def create_key(self, user_id: str, label: str) -> str:
        """Generate ak_... key, save state."""
        key = f"ak_{secrets.token_hex(24)}"

        model = APIKeyModel(user_id=user_id, label=label, created_at=time.time(), enabled=True)

        def add(keys: dict[str, Any]) -> None:
            keys[key] = model.model_dump()

        state = self.store.update(add)
        if state is None:
            raise StoreUnreadable(f"Could not persist new API key: {self.store.file_path} is unreadable")
        self._keys = state
        return key

    def verify(self, key: str) -> dict[str, Any] | None:
        """Check key validity and enabled status against the current state.

        Update last_used timestamp and save() on success.
        Return {"user_id": ..., "label": ...}.
        """
        result: dict[str, Any] | None = None

        def touch(keys: dict[str, Any]) -> None:
            nonlocal result
            data = keys.get(key)
            if not data:
                return

            model = APIKeyModel(**data)
            if not model.enabled:
                return

            model.last_used = time.time()
            keys[key] = model.model_dump()
            result = {"user_id": model.user_id, "label": model.label}

        state = self.store.update(touch)
        if state is None:
            raise StoreUnreadable(f"Could not check API key: {self.store.file_path} is unreadable")
        self._keys = state
        return result

    def revoke(self, key: str) -> bool:
        """Set enabled=False, save."""
        revoked = False

        def disable(keys: dict[str, Any]) -> None:
            nonlocal revoked
            data = keys.get(key)
            if not data:
                return

            model = APIKeyModel(**data)
            model.enabled = False
            keys[key] = model.model_dump()
            revoked = True

        state = self.store.update(disable)
        if state is None:
            raise StoreUnreadable(f"Could not revoke API key: {self.store.file_path} is unreadable")
        self._keys = state
        return revoked

    def delete_key(self, key: str) -> bool:
        """Remove key from the current on-disk state."""
        removed = False

        def remove(keys: dict[str, Any]) -> None:
            nonlocal removed
            if key in keys:
                del keys[key]
                removed = True

        state = self.store.update(remove)
        if state is None:
            raise StoreUnreadable(f"Could not delete API key: {self.store.file_path} is unreadable")
        self._keys = state
        return removed

    def list_keys(self) -> list[dict[str, Any]]:
        """Return masked keys with metadata, read from the current on-disk state."""
        self._keys = self.store.load_strict()
        result = []
        for key, data in self._keys.items():
            model = APIKeyModel(**data)
            masked_key = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"

            entry = model.model_dump()
            entry["key"] = masked_key
            result.append(entry)
        return result
