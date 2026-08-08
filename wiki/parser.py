from __future__ import annotations
import time
from pathlib import Path
from typing import Any

import frontmatter
from .models import WikiEntry


class WikiParser:
    @staticmethod
    def parse(text: str, file_path: Path | None = None) -> WikiEntry:
        try:
            post = frontmatter.loads(text)
            metadata = post.metadata
            content = post.content.strip()
        except Exception:
            metadata = {}
            content = text.strip()

        title = str(metadata.get("title") or (file_path.stem if file_path else "Untitled"))

        def to_float(val: Any, default: float = 0.5) -> float:
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        importance = to_float(metadata.get("importance"))
        wiki_type = str(metadata.get("wiki_type", "note"))
        tags = metadata.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        elif not isinstance(tags, list):
            tags = []

        now = time.time()
        created_at = to_float(metadata.get("created_at"), now)
        updated_at = to_float(metadata.get("updated_at"), now)

        entry_id = metadata.get("entry_id")
        validated_id = int(entry_id) if isinstance(entry_id, (int, float, str)) and str(entry_id).isdigit() else None

        return WikiEntry(
            entry_id=validated_id,
            wiki_type=wiki_type,
            title=title,
            content=content,
            file_path=str(file_path) if file_path else "",
            tags=tags,
            importance=importance,
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def to_markdown(entry: WikiEntry) -> str:
        metadata: dict[str, Any] = {
            "wiki_type": entry.wiki_type,
            "title": entry.title,
            "tags": entry.tags,
            "importance": entry.importance,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
        }
        if entry.entry_id is not None:
            metadata["entry_id"] = entry.entry_id

        post = frontmatter.Post(entry.content, **metadata)
        return frontmatter.dumps(post)
