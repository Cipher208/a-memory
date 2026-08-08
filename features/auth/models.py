from __future__ import annotations

import time

from pydantic import BaseModel, Field


class APIKeyModel(BaseModel):
    user_id: str
    label: str = ""
    created_at: float = Field(default_factory=time.time)
    last_used: float | None = None
    enabled: bool = True


class AuthTokenModel(BaseModel):
    token: str
    created_at: float = Field(default_factory=time.time)
