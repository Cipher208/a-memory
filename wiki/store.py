"""
File Storage for Wiki — handles disk I/O and path safety.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from shared.path_safety import safe_resolve


class WikiStore:
    """Manages physical .md files on disk with async wrappers."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def write(self, relative_path: str, content: str) -> Path:
        """Write content to file securely."""
        full_path = safe_resolve(self.base_dir, relative_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        def _sync_write():
            full_path.write_text(content, encoding="utf-8")
            return full_path

        return await asyncio.to_thread(_sync_write)

    async def read(self, relative_path: str) -> str:
        """Read file content."""
        full_path = safe_resolve(self.base_dir, relative_path)

        def _sync_read():
            return full_path.read_text(encoding="utf-8")

        return await asyncio.to_thread(_sync_read)

    async def delete(self, relative_path: str) -> bool:
        """Delete file from disk."""
        full_path = safe_resolve(self.base_dir, relative_path)

        def _sync_delete():
            if full_path.exists():
                full_path.unlink()
                return True
            return False

        return await asyncio.to_thread(_sync_delete)

    async def list_files(self, pattern: str = "**/*.md") -> AsyncIterator[Path]:
        """Glob files in directory."""

        def _sync_glob():
            return list(self.base_dir.glob(pattern))

        files = await asyncio.to_thread(_sync_glob)
        for f in files:
            yield f

    async def exists(self, relative_path: str) -> bool:
        """Check if file exists on disk."""
        full_path = safe_resolve(self.base_dir, relative_path)
        return await asyncio.to_thread(full_path.exists)
