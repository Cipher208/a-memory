from __future__ import annotations
from pydantic import BaseModel, Field


class WikiEntry(BaseModel):
    entry_id: int | None = None
    wiki_type: str
    title: str
    content: str
    file_path: str
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    # A1.2 lifecycle: active | stale | archived (default active)
    status: str = "active"
    created_at: float
    updated_at: float
