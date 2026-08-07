"""
Authentication — API key + persistent Bearer token with envelope encryption.
Bearer token and API keys are encrypted at rest using libsodium secretbox.
"""

import contextlib
import json
import logging
import os
import secrets
import time
import warnings
from pathlib import Path
from typing import Any

from features.secrets import decrypt_json, encrypt_json

logger = logging.getLogger(__name__)


class APIKeyAuth:
    """API key authentication with encrypted file persistence."""

    def __init__(self, keys_file: str | None = None):
        self.keys_file = Path(keys_file or str(Path.home() / ".mcp-ariel-memory" / "api_keys.json"))
        self.keys_file.parent.mkdir(parents=True, exist_ok=True)
        self._keys: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.keys_file.exists():
            return {}
        try:
            with Path(self.keys_file, "rb").open() as f:
                blob = f.read()
        except Exception as e:
            logger.warning("Failed to read %s: %s", self.keys_file, e)
            return {}

        # Try decrypt first (handles nonce collision where is_encrypted_blob returns False)
        try:
            return decrypt_json(blob)
        except Exception:
            pass

        # Not encrypted — parse as legacy JSON and rotate
        try:
            warnings.warn(
                f"{self.keys_file} is plain JSON; rotating to encrypted form",
                DeprecationWarning,
                stacklevel=2,
            )
            legacy = json.loads(blob.decode("utf-8"))
        except Exception as e:
            logger.warning("Failed to load %s: %s", self.keys_file, e)
            return {}

        # Rotate outside try/except — _save failure must not mask the loaded data
        try:
            self._save(legacy)
        except Exception as e:
            logger.warning("Failed to encrypt legacy file %s: %s", self.keys_file, e)
        return legacy

    def _save(self, data: dict[str, dict] | None = None) -> None:
        """Atomic write: tmp + rename + chmod 600."""
        if data is None:
            data = self._keys
        tmp_file = self.keys_file.with_suffix(".json.tmp")
        ciphertext = encrypt_json(data)
        with Path(tmp_file, "wb").open() as f:
            f.write(ciphertext)
        with contextlib.suppress(OSError):
            os.chmod(tmp_file, 0o600)
        tmp_file.replace(self.keys_file)
        with contextlib.suppress(OSError):
            os.chmod(self.keys_file, 0o600)

    def create_key(self, user_id: str, label: str = "") -> str:
        key = "ak_" + secrets.token_hex(24)
        self._keys[key] = {
            "user_id": user_id,
            "label": label,
            "created_at": time.time(),
            "last_used": None,
            "enabled": True,
        }
        self._save()
        return key

    def verify(self, key: str) -> dict[str, Any] | None:
        entry = self._keys.get(key)
        if not entry or not entry.get("enabled", True):
            return None
        return {"user_id": entry["user_id"], "label": entry.get("label", "")}

    def revoke(self, key: str) -> bool:
        if key not in self._keys:
            return False
        self._keys[key]["enabled"] = False
        self._save()
        return True

    def list_keys(self) -> list:
        return [
            {
                "key": k[:8] + "...",
                "user_id": v["user_id"],
                "label": v.get("label", ""),
                "enabled": v.get("enabled", True),
                "created_at": v["created_at"],
            }
            for k, v in self._keys.items()
        ]

    def delete_key(self, key: str) -> bool:
        if key not in self._keys:
            return False
        del self._keys[key]
        self._save()
        return True


class BearerAuth:
    """Bearer token authentication with encrypted persistence."""

    def __init__(self, token_file: str | None = None):
        self.token_file = Path(token_file or str(Path.home() / ".mcp-ariel-memory" / "bearer_token.json"))
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token = self._load_or_create()

    def _load_or_create(self) -> str:
        # 1. From env variable
        env_token = os.environ.get("MCP_AUTH_TOKEN", "")
        if env_token:
            return env_token

        # 2. From file
        if self.token_file.exists():
            try:
                with Path(self.token_file, "rb").open() as f:
                    blob = f.read()
            except Exception as e:
                logger.warning("Failed to read bearer token file %s: %s", self.token_file, e)
                blob = None

            if blob:
                # Try decrypt first (handles nonce collision)
                try:
                    data = decrypt_json(blob)
                    return data.get("token", "")
                except Exception:
                    pass

                # Not encrypted — parse as legacy JSON and rotate
                try:
                    warnings.warn(
                        f"{self.token_file} is plain JSON; rotating to encrypted form",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                    data = json.loads(blob.decode("utf-8"))
                    token = data.get("token", "")
                    if token:
                        self._save(token)
                    return token
                except Exception as e:
                    logger.warning("Failed to load bearer token from %s: %s", self.token_file, e)

        # 3. Create new and save
        token = f"mt_{secrets.token_hex(32)}"
        self._save(token)
        return token

    def _save(self, token: str) -> None:
        """Atomic write: tmp + rename + chmod 600."""
        data = {"token": token, "created_at": time.time()}
        ciphertext = encrypt_json(data)
        tmp_file = self.token_file.with_suffix(".json.tmp")
        with Path(tmp_file, "wb").open() as f:
            f.write(ciphertext)
        with contextlib.suppress(OSError):
            os.chmod(tmp_file, 0o600)
        tmp_file.replace(self.token_file)
        with contextlib.suppress(OSError):
            os.chmod(self.token_file, 0o600)

    def verify(self, auth_header: str) -> bool:
        if not auth_header:
            return False
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            return token == self._token
        return False

    def get_token(self) -> str:
        return self._token

    def rotate(self) -> str:
        """Create a new token (old one stops working)."""
        self._token = f"mt_{secrets.token_hex(32)}"
        self._save(self._token)
        return self._token


# Singletons
api_key_auth = APIKeyAuth()
bearer_auth = BearerAuth()
