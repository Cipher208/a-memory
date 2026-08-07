"""
WikiManager — unified wiki system with layer-based separation.
Uses WikiParser for content and WikiStore for disk I/O.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from wiki.shared import get_enabled_types, get_external_dirs, parse_tags
from wiki.parser import WikiParser
from wiki.store import WikiStore
from shared.path_safety import safe_resolve


@dataclass
class WikiEntry:
    entry_id: int
    wiki_type: str
    title: str
    content: str
    file_path: str
    tags: list[str]
    importance: float
    created_at: float
    updated_at: float


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
    """Unified wiki manager: coordinates parser, store and DB index."""

    def __init__(self, layer: str = "user", base_dir: str | None = None, cm: AsyncConnectionManager | None = None):
        self.layer = layer
        self.base_path = Path(base_dir or str(Path.home() / ".mcp-ariel-memory" / "wiki" / layer))
        self.store = WikiStore(self.base_path)
        self.parser = WikiParser()
        self._cm = cm or connection_manager

    async def init_db(self):
        """Initialize SQLite tables for wiki indexing."""
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS wiki_index (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL,
                wiki_type TEXT NOT NULL,
                title TEXT NOT NULL,
                file_path TEXT NOT NULL,
                tags TEXT,
                importance REAL DEFAULT 0.5,
                content TEXT DEFAULT '',
                content_hash TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_path ON wiki_index(file_path);
            CREATE INDEX IF NOT EXISTS idx_wiki_layer ON wiki_index(layer);
            CREATE INDEX IF NOT EXISTS idx_wiki_type ON wiki_index(wiki_type);
            CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki_index(updated_at);
            
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
                title, content, wiki_type, tags,
                content=wiki_index,
                content_rowid=entry_id
            );
        """,
        )

    async def add(self, wiki_type: str, title: str, content: str, tags: list[str] | None = None, importance: float = 0.5) -> str:
        """Add new wiki page."""
        enabled = self.get_enabled_types()
        if enabled and wiki_type not in enabled:
            raise ValueError(f"Wiki type '{wiki_type}' is disabled. Enabled: {enabled}")

        safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip().replace(" ", "_")
        file_path = self.base_path / wiki_type / f"{safe_title}.md"

        md_content = self.parser.to_md(title, content, tags, importance)
        # Use store but with absolute path for logic consistency
        await self.store.write(str(file_path.relative_to(self.base_path)), md_content)

        await self._index_file(file_path, wiki_type, title, content, tags, importance)
        return str(file_path)

    async def update(self, file_path: str, title: str | None = None, content: str | None = None, tags: list[str] | None = None, importance: float | None = None):
        """Update existing wiki page."""
        p = safe_resolve(self.base_path, file_path)
        if not await self.store.exists(str(p.relative_to(self.base_path))):
            return

        raw_text = await self.store.read(str(p.relative_to(self.base_path)))
        existing = self.parser.parse(raw_text)
        
        new_title = title or existing["title"] or p.stem
        new_content = content or existing["content"]
        new_tags = tags if tags is not None else existing["tags"]
        new_importance = importance if importance is not None else existing["importance"]

        md_content = self.parser.to_md(new_title, new_content, new_tags, new_importance)
        await self.store.write(str(p.relative_to(self.base_path)), md_content)

        wiki_type = p.parent.name or "diary"
        await self._index_file(p, wiki_type, new_title, new_content, new_tags, new_importance)

    async def get(self, file_path: str) -> WikiEntry | None:
        """Retrieve wiki entry by path."""
        try:
            p = safe_resolve(self.base_path, file_path)
        except (ValueError, RuntimeError):
            return None

        if not await self.store.exists(str(p.relative_to(self.base_path))):
            return None

        raw_text = await self.store.read(str(p.relative_to(self.base_path)))
        parsed = self.parser.parse(raw_text)
        
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute("SELECT * FROM wiki_index WHERE file_path=?", (str(p),))
        row = await cur.fetchone()
        
        if row:
            return WikiEntry(
                entry_id=row["entry_id"],
                wiki_type=row["wiki_type"],
                title=parsed["title"] or p.stem,
                content=parsed["content"],
                file_path=str(p),
                tags=parsed["tags"],
                importance=parsed["importance"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        return None

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """FTS5 search across all indexed files."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            """SELECT wi.entry_id, wi.wiki_type, wi.file_path, wi.tags, wi.importance, fts.rank
                FROM wiki_fts fts JOIN wiki_index wi ON fts.rowid = wi.entry_id
                WHERE wiki_fts MATCH ? AND wi.layer = ?
                ORDER BY fts.rank DESC LIMIT ?""",
            (query, self.layer, limit),
        )
        rows = await cur.fetchall()
        results = []
        for r in rows:
            entry = await self.get(r["file_path"])
            if entry:
                results.append({
                    "id": entry.entry_id,
                    "type": entry.wiki_type,
                    "title": entry.title,
                    "content": entry.content[:500],
                    "file_path": entry.file_path,
                    "tags": entry.tags,
                    "importance": entry.importance,
                    "score": abs(r["rank"]) if r["rank"] else 0,
                })
        return results

    async def list_by_type(self, wiki_type: str, limit: int = 20) -> list[WikiEntry]:
        """List wiki entries by type."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT file_path FROM wiki_index WHERE layer=? AND wiki_type=? ORDER BY updated_at DESC LIMIT ?",
            (self.layer, wiki_type, limit),
        )
        rows = await cur.fetchall()
        entries = []
        for r in rows:
            entry = await self.get(r["file_path"])
            if entry:
                entries.append(entry)
        return entries

    async def list_all(self, limit: int = 50) -> list[WikiEntry]:
        """List all wiki entries for current layer."""
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT file_path FROM wiki_index WHERE layer=? ORDER BY updated_at DESC LIMIT ?",
            (self.layer, limit),
        )
        rows = await cur.fetchall()
        entries = []
        for r in rows:
            entry = await self.get(r["file_path"])
            if entry:
                entries.append(entry)
        return entries

    async def delete(self, file_path: str) -> bool:
        """Delete wiki page and remove from index."""
        p = safe_resolve(self.base_path, file_path)
        await self.store.delete(str(p.relative_to(self.base_path)))
        
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute("DELETE FROM wiki_index WHERE file_path=?", (str(p),))
        await conn.commit()
        return cur.rowcount > 0

    async def count(self, wiki_type: str | None = None) -> int:
        """Total pages in current layer."""
        conn = await self._cm.get(DB_NAME)
        if wiki_type:
            sql = "SELECT COUNT(*) FROM wiki_index WHERE layer=? AND wiki_type=?"
            params = (self.layer, wiki_type)
        else:
            sql = "SELECT COUNT(*) FROM wiki_index WHERE layer=?"
            params = (self.layer,)
        
        row = await (await conn.execute(sql, params)).fetchone()
        return row[0] if row else 0

    def get_enabled_types(self) -> list[str]:
        all_types = LAYER_TYPES.get(self.layer, ALL_USER_TYPES)
        return get_enabled_types(self.layer, all_types)

    async def sync_external(self, external_dirs: list[str] | None = None) -> dict[str, int]:
        """Import external .md files into wiki."""
        dirs = external_dirs or get_external_dirs(self.layer)
        result = {"imported": 0, "skipped": 0, "errors": 0}
        
        for dir_path in dirs:
            p = Path(dir_path)
            if not p.exists(): continue
            
            for md_file in p.glob("**/*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    parsed = self.parser.parse(content)
                    wiki_type = self._guess_type(md_file, parsed["content"])
                    
                    if wiki_type not in self.get_enabled_types():
                        result["skipped"] += 1
                        continue

                    title = parsed["title"] or md_file.stem
                    await self.add(wiki_type, title, parsed["content"], parsed["tags"], parsed["importance"])
                    result["imported"] += 1
                except Exception:
                    result["errors"] += 1
        return result

    async def _index_file(self, file_path: Path, wiki_type: str, title: str, content: str, tags: list[str] | None = None, importance: float = 0.5):
        """Update DB index for a file."""
        import hashlib
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        now = time.time()
        conn = await self._cm.get(DB_NAME)
        
        cur = await conn.execute("SELECT entry_id, content_hash FROM wiki_index WHERE file_path=?", (str(file_path),))
        existing = await cur.fetchone()

        if existing and existing["content_hash"] == content_hash:
            return

        tags_json = json.dumps(tags or [])
        if existing:
            await conn.execute(
                "UPDATE wiki_index SET title=?, tags=?, importance=?, content=?, content_hash=?, updated_at=? WHERE entry_id=?",
                (title, tags_json, importance, content, content_hash, now, existing["entry_id"]),
            )
            # FTS update
            await conn.execute("INSERT INTO wiki_fts(wiki_fts, rowid, title, content, wiki_type, tags) VALUES ('delete', ?, ?, ?, ?, ?)",
                             (existing["entry_id"], title, content, wiki_type, tags_json))
            await conn.execute("INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
                             (existing["entry_id"], title, content, wiki_type, tags_json))
        else:
            cur = await conn.execute(
                """INSERT INTO wiki_index (layer, wiki_type, title, file_path, tags, importance, content, content_hash, created_at, updated_at) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.layer, wiki_type, title, str(file_path), tags_json, importance, content, content_hash, now, now)
            )
            await conn.execute("INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
                             (cur.lastrowid, title, content, wiki_type, tags_json))
        await conn.commit()

    def _guess_type(self, path: Path, content: str) -> str:
        """Heuristic to determine wiki type based on file name/content."""
        name = path.stem.lower()
        all_types = LAYER_TYPES.get(self.layer, ALL_USER_TYPES)

        for t in all_types:
            if t in name or t in path.parent.name.lower():
                return t

        if self.layer == "user":
            if any(w in content.lower() for w in ["дневник", "diary", "сегодня"]): return "diary"
            if any(w in content.lower() for w in ["проект", "задача"]): return "work_notes"
            return "diary"
        
        if any(w in content.lower() for w in ["решение", "decided"]): return "decision_log"
        if any(w in content.lower() for w in ["ошибка", "error"]): return "error_analysis"
        return "wiki_agent"
