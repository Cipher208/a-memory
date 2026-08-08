from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class RAGPage(BaseModel):
    id: int | None = None
    layer: str = "user"
    user_id: str = "default"
    title: str
    path: str | None = None
    content: str
    sha256_hash: str | None = None
    wiki_type: str | None = None
    created_at: float | None = Field(default_factory=lambda: datetime.now(tz=timezone.utc).timestamp())
    updated_at: float | None = Field(default_factory=lambda: datetime.now(tz=timezone.utc).timestamp())


class RAGChunk(BaseModel):
    id: int | None = None
    page_id: int
    chunk_index: int
    content: str
    bin_embedding: bytes | None = None


class SearchResult(BaseModel):
    page_id: int
    title: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
