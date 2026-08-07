"""
WikiManager — unified wiki system with layer-based separation.
Architecture: .md files on disk = primary, SQLite FTS5 = search index.
Layers: user, agent, shared.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.path_safety import safe_resolve
from wiki.shared import (
    get_enabled_types,
    get_external_dirs,
    parse_tags,
)


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
    """Unified wiki: .md files on disk + SQLite FTS5 index, parameterized by layer."""

    def __init__(self, layer: str = "user", base_dir: str | None = None, cm: AsyncConnectionManager | None = None):
        self.layer = layer
        self.base_dir = Path(base_dir or str(Path.home() / ".mcp-ariel-memory" / "wiki" / layer))
        self._cm = cm or connection_manager
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def init_db(self):
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
        """,
        )
        conn = await self._cm.get(DB_NAME)
        await conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
                title, content, wiki_type, tags,
                content=wiki_index,
                content_rowid=entry_id
            )
        """)
        await conn.commit()

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

        md_content = self._to_md(title, content, tags, importance)
        file_path.write_text(md_content, encoding="utf-8")

        await self._index_file(file_path, wiki_type, title, content, tags, importance)
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
        if not p.exists():
            return

        existing = self._parse_md(p.read_text(encoding="utf-8"))
        new_title = title or existing.get("title", p.stem)
        new_content = content or existing.get("content", "")
        new_tags = tags if tags is not None else existing.get("tags", [])
        new_importance = importance if importance is not None else existing.get("importance", 0.5)

        md_content = self._to_md(new_title, new_content, new_tags, new_importance)
        p.write_text(md_content, encoding="utf-8")

        wiki_type = p.parent.name
        await self._index_file(p, wiki_type, new_title, new_content, new_tags, new_importance)

    async def get(self, file_path: str) -> WikiEntry | None:
        try:
            p = safe_resolve(self.base_dir, file_path)
        except ValueError:
            return None
        if not p.exists():
            return None
        parsed = self._parse_md(p.read_text(encoding="utf-8"))
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute("SELECT * FROM wiki_index WHERE file_path=?", (str(p),))
        row = await cur.fetchone()
        if row:
            return WikiEntry(
                entry_id=row["entry_id"],
                wiki_type=row["wiki_type"],
                title=parsed["title"],
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
        try:
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
                p = Path(r["file_path"])
                if p.exists():
                    parsed = self._parse_md(p.read_text(encoding="utf-8"))
                    results.append(
                        {
                            "id": r["entry_id"],
                            "type": r["wiki_type"],
                            "title": parsed["title"],
                            "content": parsed["content"][:500],
                            "file_path": str(p),
                            "tags": parse_tags(r["tags"]),
                            "importance": r["importance"],
                            "score": abs(r["rank"]) if r["rank"] else 0,
                        }
                    )
            return results
        except Exception:
            return []

    async def list_by_type(self, wiki_type: str, limit: int = 20) -> list[WikiEntry]:
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM wiki_index WHERE layer=? AND wiki_type=? ORDER BY updated_at DESC LIMIT ?",
            (self.layer, wiki_type, limit),
        )
        rows = await cur.fetchall()
        entries = []
        for r in rows:
            p = Path(r["file_path"])
            if p.exists():
                parsed = self._parse_md(p.read_text(encoding="utf-8"))
                entries.append(
                    WikiEntry(
                        entry_id=r["entry_id"],
                        wiki_type=r["wiki_type"],
                        title=parsed["title"],
                        content=parsed["content"],
                        file_path=str(p),
                        tags=parsed["tags"],
                        importance=parsed["importance"],
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                    )
                )
        return entries

    async def list_all(self, limit: int = 50) -> list[WikiEntry]:
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute("SELECT * FROM wiki_index WHERE layer=? ORDER BY updated_at DESC LIMIT ?", (self.layer, limit))
        rows = await cur.fetchall()
        entries = []
        for r in rows:
            p = Path(r["file_path"])
            if p.exists():
                parsed = self._parse_md(p.read_text(encoding="utf-8"))
                entries.append(
                    WikiEntry(
                        entry_id=r["entry_id"],
                        wiki_type=r["wiki_type"],
                        title=parsed["title"],
                        content=parsed["content"],
                        file_path=str(p),
                        tags=parsed["tags"],
                        importance=parsed["importance"],
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                    )
                )
        return entries

    async def delete(self, file_path: str) -> bool:
        p = safe_resolve(self.base_dir, file_path)
        if p.exists():
            p.unlink()
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute("DELETE FROM wiki_index WHERE file_path=?", (str(p),))
        await conn.commit()
        return cur.rowcount > 0

    async def count(self, wiki_type: str | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if wiki_type:
            cur = await conn.execute("SELECT COUNT(*) FROM wiki_index WHERE layer=? AND wiki_type=?", (self.layer, wiki_type))
        else:
            cur = await conn.execute("SELECT COUNT(*) FROM wiki_index WHERE layer=?", (self.layer,))
        row = await cur.fetchone()
        return row[0] if row else 0

    def get_enabled_types(self) -> list[str]:
        return self._get_enabled_types()

    def get_external_dirs(self) -> list[str]:
        return get_external_dirs(self.layer)

    async def reindex_all(self) -> dict[str, int]:
        """Re-index all .md files from disk to DB."""
        result = {"indexed": 0, "skipped": 0, "errors": 0}
        for wiki_type_dir in self.base_dir.iterdir():
            if not wiki_type_dir.is_dir():
                continue
            wiki_type = wiki_type_dir.name
            for md_file in wiki_type_dir.glob("*.md"):
                try:
                    parsed = self._parse_md(md_file.read_text(encoding="utf-8"))
                    await self._index_file(md_file, wiki_type, parsed["title"], parsed["content"], parsed["tags"], parsed["importance"])
                    result["indexed"] += 1
                except Exception:
                    result["errors"] += 1
        return result

    async def sync_external(self, external_dirs: list[str] | None = None) -> dict[str, int]:
        """Import external .md files into wiki."""
        dirs = external_dirs or self.get_external_dirs()
        result = {"imported": 0, "skipped": 0, "errors": 0}
        for dir_path in dirs:
            p = Path(dir_path)
            if not p.exists():
                continue
            for md_file in p.glob("**/*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    parsed = self._parse_md(content)
                    title = parsed["title"] or md_file.stem
                    wiki_type = self._guess_type(md_file, parsed["content"])
                    if wiki_type not in self._get_enabled_types():
                        result["skipped"] += 1
                        continue

                    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip().replace(" ", "_")
                    dest = self._type_dir(wiki_type) / f"{safe_title}.md"
                    dest.write_text(content, encoding="utf-8")
                    await self._index_file(dest, wiki_type, title, parsed["content"], parsed["tags"], parsed["importance"])
                    result["imported"] += 1
                except Exception:
                    result["errors"] += 1
        return result

    async def _index_file(self, file_path: Path, wiki_type: str, title: str, content: str, tags: list[str] | None = None, importance: float = 0.5):
        """Index a single .md file into DB."""
        import hashlib

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        now = time.time()

        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute("SELECT entry_id, content_hash FROM wiki_index WHERE file_path=?", (str(file_path),))
        existing = await cur.fetchone()

        if existing and existing["content_hash"] == content_hash:
            return

        if existing:
            await conn.execute(
                "UPDATE wiki_index SET title=?, tags=?, importance=?, content=?, content_hash=?, updated_at=? WHERE entry_id=?",
                (title, json.dumps(tags or []), importance, content, content_hash, now, existing["entry_id"]),
            )
            entry_id = existing["entry_id"]
            await conn.execute(
                "INSERT INTO wiki_fts(wiki_fts, rowid, title, content, wiki_type, tags) VALUES ('delete', ?, ?, ?, ?, ?)",
                (entry_id, title, content, wiki_type, json.dumps(tags or [])),
            )
            await conn.execute(
                "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
                (entry_id, title, content, wiki_type, json.dumps(tags or [])),
            )
        else:
            cur = await conn.execute(
                "INSERT INTO wiki_index (layer, wiki_type, title, file_path, tags, importance, content, content_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.layer,
                    wiki_type,
                    title,
                    str(file_path),
                    json.dumps(tags or []),
                    importance,
                    content,
                    content_hash,
                    now,
                    now,
                ),
            )
            entry_id = cur.lastrowid
            await conn.execute(
                "INSERT INTO wiki_fts(rowid, title, content, wiki_type, tags) VALUES (?, ?, ?, ?, ?)",
                (entry_id, title, content, wiki_type, json.dumps(tags or [])),
            )

        await conn.commit()

    def _parse_md(self, text: str) -> dict[str, Any]:
        """Parse .md with YAML frontmatter."""
        result: dict[str, Any] = {"title": "", "content": text, "tags": [], "importance": 0.5}
        if text.startswith("---"):
            try:
                end = text.index("---", 3)
                frontmatter = text[3:end].strip()
                body = text[end + 3 :].strip()
                for line in frontmatter.split("\n"):
                    if ":" in line:
                        key, val = line.split(":", 1)
                        key = key.strip()
                        val = val.strip()
                        if key == "tags":
                            result["tags"] = [t.strip().strip('"').strip("'") for t in val.split(",")]
                        elif key == "importance":
                            result["importance"] = float(val)
                        elif key == "title":
                            result["title"] = val.strip('"').strip("'")
                result["content"] = body
            except (ValueError, IndexError):
                pass

        if not result["title"]:
            for line in result["content"].split("\n"):
                if line.startswith("# "):
                    result["title"] = line[2:].strip()
                    break

        return result

    def _to_md(self, title: str, content: str, tags: list[str] | None = None, importance: float = 0.5) -> str:
        """Create .md with YAML frontmatter."""
        lines = ["---"]
        lines.append(f'title: "{title}"')
        if tags:
            lines.append("tags: " + ", ".join(tags))
        lines.append(f"importance: {importance}")
        lines.append(f"updated: {time.strftime('%Y-%m-%dT%H:%M:%S')}")
        lines.append("---")
        lines.append("")
        if not content.startswith("# "):
            lines.append(f"# {title}")
            lines.append("")
        lines.append(content)
        return "\n".join(lines)

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
        else:
            if any(w in content.lower() for w in ["решение", "decided"]):
                return "decision_log"
            if any(w in content.lower() for w in ["ошибка", "error"]):
                return "error_analysis"
            return "wiki_agent"
