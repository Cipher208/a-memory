from __future__ import annotations
import hashlib
import logging
import time
import asyncio
from pathlib import Path
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.path_safety import safe_resolve
from wiki.models import WikiEntry
from wiki.parser import WikiParser
from wiki.index import WikiIndex
from wiki.shared import (
    get_enabled_types,
    get_external_dirs,
)

logger = logging.getLogger(__name__)

ALL_USER_TYPES = ["diary", "relationships", "desires", "aspirations", "work_notes", "preferences", "retrospective"]
ALL_AGENT_TYPES = [
    "decision_log",
    "error_analysis",
    "personality_evolution",
    "emotional_context",
    "wiki_agent",
    "learning_journal",
    "principle_log",
]

LAYER_TYPES = {
    "user": ALL_USER_TYPES,
    "agent": ALL_AGENT_TYPES,
    "shared": ALL_USER_TYPES + ALL_AGENT_TYPES,
}


class WikiManager:
    """Unified wiki orchestrator: coordinates WikiParser (I/O) and WikiIndex (DB)."""

    def __init__(self, layer: str = "user", base_dir: str | None = None, cm: AsyncConnectionManager | None = None):
        self.layer = layer
        self.base_dir = Path(base_dir or str(Path.home() / ".mcp-ariel-memory" / "wiki" / layer))
        self._cm = cm or connection_manager
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index = WikiIndex(self._cm, layer)
        self.parser = WikiParser()

    async def init_db(self):
        """Delegate to index layer."""
        await self.index.init_db()

    def _get_enabled_types(self) -> list[str]:
        all_types = LAYER_TYPES.get(self.layer, ALL_USER_TYPES)
        return get_enabled_types(self.layer, all_types)

    def _type_dir(self, wiki_type: str) -> Path:
        d = self.base_dir / wiki_type
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def add(self, wiki_type: str, title: str, content: str, tags: list[str] | None = None, importance: float = 0.5) -> str:
        """Create .md file and index it. Returns file path."""
        enabled = self._get_enabled_types()
        if enabled and wiki_type not in enabled:
            raise ValueError(f"Wiki type '{wiki_type}' is disabled. Enabled: {enabled}")

        safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip().replace(" ", "_")
        file_path = self._type_dir(wiki_type) / f"{safe_title}.md"

        now = time.time()
        entry = WikiEntry(
            wiki_type=wiki_type,
            title=title,
            content=content,
            file_path=str(file_path),
            tags=tags or [],
            importance=importance,
            created_at=now,
            updated_at=now,
        )

        md_content = self.parser.to_markdown(entry)
        await asyncio.to_thread(file_path.write_text, md_content, encoding="utf-8")

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        await self.index.save(entry, content_hash)
        return str(file_path)

    async def update(
        self,
        file_path: str,
        title: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
    ):
        """Update .md file and re-index."""
        p = safe_resolve(self.base_dir, file_path)
        if not await asyncio.to_thread(p.exists):
            return

        text = await asyncio.to_thread(p.read_text, encoding="utf-8")
        entry = self.parser.parse(text, p)

        if title is not None:
            entry.title = title
        if content is not None:
            entry.content = content
        if tags is not None:
            entry.tags = tags
        if importance is not None:
            entry.importance = importance

        entry.updated_at = time.time()

        md_content = self.parser.to_markdown(entry)
        await asyncio.to_thread(p.write_text, md_content, encoding="utf-8")

        content_hash = hashlib.sha256(entry.content.encode()).hexdigest()
        await self.index.save(entry, content_hash)

    async def get(self, file_path: str) -> WikiEntry | None:
        """Fetch entry from disk, validated by existence."""
        try:
            p = safe_resolve(self.base_dir, file_path)
        except ValueError:
            return None

        if not await asyncio.to_thread(p.exists):
            return None

        text = await asyncio.to_thread(p.read_text, encoding="utf-8")
        return self.parser.parse(text, p)

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Delegate search to index."""
        results = await self.index.search(query, limit)
        for r in results:
            # Add snippet if missing
            if "content" in r:
                r["content"] = r["content"][:500]
        return results

    async def list_by_type(self, wiki_type: str, limit: int = 20) -> list[WikiEntry]:
        """List entries of a specific type."""
        rows = await self.index.list_by_type(wiki_type, limit)
        return await self._rows_to_entries(rows)

    async def list_all(self, limit: int = 50) -> list[WikiEntry]:
        """List all entries in the layer."""
        rows = await self.index.list_all(limit)
        return await self._rows_to_entries(rows)

    async def _rows_to_entries(self, rows: list[dict]) -> list[WikiEntry]:
        entries = []
        for r in rows:
            entry = await self.get(r["file_path"])
            if entry:
                entries.append(entry)
        return entries

    async def delete(self, file_path: str) -> bool:
        """Delete from FS and Index."""
        p = safe_resolve(self.base_dir, file_path)
        if await asyncio.to_thread(p.exists):
            await asyncio.to_thread(p.unlink)

        # index.delete handles both wiki_index and wiki_fts
        await self.index.delete(str(p))
        return True

    async def count(self, wiki_type: str | None = None) -> int:
        return await self.index.count(wiki_type)

    def get_enabled_types(self) -> list[str]:
        return self._get_enabled_types()

    def get_external_dirs(self) -> list[str]:
        return get_external_dirs(self.layer)

    async def reindex_all(self) -> dict[str, int]:
        """Re-index all .md files from disk to DB using batching and hash checks."""
        result = {"indexed": 0, "skipped": 0, "errors": 0}

        md_files = []
        enabled_types = self._get_enabled_types()

        def _collect_files():
            files = []
            for wiki_type in enabled_types:
                type_dir = self.base_dir / wiki_type
                if type_dir.exists() and type_dir.is_dir():
                    files.extend(list(type_dir.glob("*.md")))
            return files

        md_files = await asyncio.to_thread(_collect_files)

        async def _process_file(f: Path):
            try:
                text = await asyncio.to_thread(f.read_text, encoding="utf-8")
                entry = self.parser.parse(text, f)
                content_hash = hashlib.sha256(entry.content.encode()).hexdigest()

                # Check hash in DB before saving
                existing = await self.index.get_by_path(str(f))
                if existing and existing.get("content_hash") == content_hash:
                    return "skipped"

                await self.index.save(entry, content_hash)
                return "indexed"
            except Exception:
                logger.exception(f"Error reindexing {f}")
                return "error"

        # Batch processing
        tasks = [_process_file(f) for f in md_files]
        if tasks:
            outcomes = await asyncio.gather(*tasks)
            for o in outcomes:
                result[o] += 1

        return result

    async def sync_external(self, external_dirs: list[str] | None = None) -> dict[str, int]:
        """Import external .md files with optimization."""
        dirs = external_dirs or self.get_external_dirs()
        result = {"imported": 0, "skipped": 0, "errors": 0}

        enabled_types = self._get_enabled_types()

        for dir_path in dirs:
            p = Path(dir_path)
            if not await asyncio.to_thread(p.exists):
                continue

            def _find_md():
                return list(p.glob("**/*.md"))

            md_files = await asyncio.to_thread(_find_md)

            async def _sync_file(f: Path):
                try:
                    content = await asyncio.to_thread(f.read_text, encoding="utf-8")
                    parsed_entry = self.parser.parse(content, f)
                    wiki_type = self._guess_type(f, parsed_entry.content)

                    if wiki_type not in enabled_types:
                        return "skipped"

                    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in parsed_entry.title).strip().replace(" ", "_")
                    dest = self._type_dir(wiki_type) / f"{safe_title}.md"

                    # Optimization: check if already exists and same content
                    if await asyncio.to_thread(dest.exists):
                        dest_content = await asyncio.to_thread(dest.read_text, encoding="utf-8")
                        if dest_content == content:
                            return "skipped"

                    await asyncio.to_thread(dest.write_text, content, encoding="utf-8")
                    content_hash = hashlib.sha256(content.encode()).hexdigest()
                    parsed_entry.file_path = str(dest)
                    parsed_entry.wiki_type = wiki_type
                    await self.index.save(parsed_entry, content_hash)
                    return "imported"
                except Exception:
                    logger.exception(f"Error syncing {f}")
                    return "error"

            tasks = [_sync_file(f) for f in md_files]
            if tasks:
                outcomes = await asyncio.gather(*tasks)
                for o in outcomes:
                    result[o] += 1

        return result

    def _guess_type(self, path: Path, content: str) -> str:
        name = path.stem.lower()
        parent = path.parent.name.lower()
        all_types = LAYER_TYPES.get(self.layer, ALL_USER_TYPES)

        for t in all_types:
            if t in name or t in parent:
                return t

        if self.layer == "user":
            if any(w in content.lower() for w in ["дневник", "diary", "сегодня"]):
                return "diary"
            if any(w in content.lower() for w in ["проект", "задача"]):
                return "work_notes"
            return "diary"
        if any(w in content.lower() for w in ["решение", "decided"]):
            return "decision_log"
        if any(w in content.lower() for w in ["ошибка", "error"]):
            return "error_analysis"
        return "wiki_agent"
