from typing import Optional
from pydantic import BaseModel, Field

class WikiEntry(BaseModel):
    entry_id: Optional[int] = None
    wiki_type: str
    title: str
    content: str
    file_path: str
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    created_at: float
    updated_at: float
