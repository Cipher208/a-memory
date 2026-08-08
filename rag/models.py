from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class RAGPage(BaseModel):
    id: Optional[int] = None
    layer: str = "user"
    user_id: str = "default"
    title: str
    path: Optional[str] = None
    content: str
    sha256_hash: Optional[str] = None
    wiki_type: Optional[str] = None
    created_at: Optional[float] = Field(default_factory=lambda: datetime.now().timestamp())
    updated_at: Optional[float] = Field(default_factory=lambda: datetime.now().timestamp())


class RAGChunk(BaseModel):
    id: Optional[int] = None
    page_id: int
    chunk_index: int
    content: str
    bin_embedding: Optional[bytes] = None


class SearchResult(BaseModel):
    page_id: int
    title: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
