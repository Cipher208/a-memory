from __future__ import annotations

import os
import secrets
from pathlib import Path

from features.auth.models import AuthTokenModel
from features.auth.store import EncryptedStore


class BearerAuth:
    def __init__(self, token_file: Path | None = None):
        if token_file is None:
            token_file = Path("data/auth/token.enc")

        self.store = EncryptedStore(token_file, AuthTokenModel)
        self._load_or_create()

    def _load_or_create(self) -> None:
        """Load token from store or create new one."""
        data = self.store.load()
        if data and "token" in data:
            self._current_token = data["token"]
        else:
            self.rotate()

    def get_token(self) -> str:
        """
        1. Check os.environ.get("MCP_AUTH_TOKEN").
        2. If not found, load from Store (already done in init/load_or_create).
        3. If still not found, create new mt_... token and save to Store.
        """
        env_token = os.environ.get("MCP_AUTH_TOKEN")
        if env_token:
            return env_token

        return self._current_token

    def verify(self, auth_header: str) -> bool:
        """Check if header matches the current token."""
        if not auth_header or not auth_header.startswith("Bearer "):
            return False

        provided_token = auth_header[7:]
        return provided_token == self.get_token()

    def rotate(self) -> str:
        """Generate new token, save to Store, return it."""
        new_token = f"mt_{secrets.token_hex(32)}"
        model = AuthTokenModel(token=new_token)
        self.store.save(model.model_dump())
        self._current_token = new_token
        return new_token
