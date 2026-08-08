import time
from pathlib import Path

import frontmatter
from .models import WikiEntry


class WikiParser:
    """Parses and generates Markdown files with YAML frontmatter."""

    @staticmethod
    def parse(text: str, file_path: Path | None = None) -> WikiEntry:
        """Parse .md with YAML frontmatter.

        Returns WikiEntry.
        """
        try:
            post = frontmatter.loads(text)
            metadata = post.metadata
            content = post.content.strip()
        except Exception:
            # Fallback for malformed YAML or other errors
            metadata = {}
            content = text.strip()

        # Extract fields from frontmatter or use defaults
        title = metadata.get("title") or (file_path.stem if file_path else "Untitled")
        try:
            importance = float(metadata.get("importance", 0.5))
        except (ValueError, TypeError):
            importance = 0.5

        wiki_type = metadata.get("wiki_type", "note")
        tags = metadata.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        elif not isinstance(tags, list):
            tags = []

        now = time.time()
        try:
            created_at = float(metadata.get("created_at", now))
        except (ValueError, TypeError):
            created_at = now

        try:
            updated_at = float(metadata.get("updated_at", now))
        except (ValueError, TypeError):
            updated_at = now

        return WikiEntry(
            entry_id=metadata.get("entry_id"),
            wiki_type=wiki_type,
            title=str(title),
            content=content,
            file_path=str(file_path) if file_path else "",
            tags=tags,
            importance=importance,
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def to_markdown(entry: WikiEntry) -> str:
        """Generate .md string with YAML frontmatter."""
        metadata = {
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
