from __future__ import annotations
import hashlib
import logging
import time
import asyncio
from pathlib import Path
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.connection import _DEFAULT_DIR
from shared.path_safety import safe_resolve
from wiki.models import WikiEntry
from wiki.parser import WikiParser
from wiki.index import WikiIndex
from wiki.shared import (
    get_enabled_types,
    get_external_dirs,
)

logger = logging.getLogger(__name__)

ALL_USER_TYPES = [
    "diary",
    "relationships",
    "desires",
    "aspirations",
    "work_notes",
    "preferences",
    "retrospective",
    "skill",  # D2.1 Skill=Memory — agent-read Markdown instructions
]
PROJECT_TYPES = ["project_spec"]  # projects are global; pages live in the user wiki
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
    "user": ALL_USER_TYPES + PROJECT_TYPES,
    "agent": ALL_AGENT_TYPES,
    "shared": ALL_USER_TYPES + ALL_AGENT_TYPES + PROJECT_TYPES,
}


class WikiManager:
    """Unified wiki orchestrator: coordinates WikiParser (I/O) and WikiIndex (DB)."""

    def __init__(
        self,
        layer: str = "user",
        base_dir: str | None = None,
        cm: AsyncConnectionManager | None = None,
        *,
        auto_fix: bool = False,
    ):
        self.layer = layer
        self._auto_fix = auto_fix
        # Same data dir the connection manager uses — wiki files must live
        # inside the instance's own MCP_MEMORY_DATA_DIR, not a shared folder.
        self.base_dir = Path(base_dir or str(Path(_DEFAULT_DIR) / "wiki" / layer))
        self._cm = cm or connection_manager
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index = WikiIndex(self._cm, layer)
        self.parser = WikiParser()
        # A2.3: optional RAG engine — when set, page writes also chunk into
        # rag_chunks (doc → searchable sections). AppContext wires its own.
        self.rag: Any | None = None

    async def _ingest_into_rag(self, entry: WikiEntry) -> None:
        """A2.3: chunk a wiki page into rag_pages/rag_chunks (fail-soft).

        sha256-deduped by the ingestor — rewrites with unchanged content are
        no-ops; changed content creates a NEW page (the old one is orphaned
        until rag cleanup — accepted ceiling, documented).
        """
        if self.rag is None:
            return
        try:
            await self.rag.ingest_text(
                title=entry.title,
                text=entry.content,
                user_id="default",
                wiki_type=entry.wiki_type,
                path=entry.file_path,
            )
        except Exception as exc:
            logger.debug("rag ingest skipped for %s: %s", entry.file_path, exc)

    async def init_db(self) -> None:
        """Delegate to index layer."""
        await self.index.init_db()

    def _get_enabled_types(self) -> list[str]:
        all_types = LAYER_TYPES.get(self.layer, ALL_USER_TYPES)
        return get_enabled_types(self.layer, all_types)

    def _type_dir(self, wiki_type: str) -> Path:
        d = self.base_dir / wiki_type
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def add(
        self,
        wiki_type: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        importance: float = 0.5,
        status: str = "active",
    ) -> str:
        """Create .md file and index it. Returns file path."""
        enabled = self._get_enabled_types()
        if enabled and wiki_type not in enabled:
            raise ValueError(f"Wiki type '{wiki_type}' is disabled. Enabled: {enabled}")

        # Length cap: a 300-char title must not raise OSError "File name too
        # long" (chaos-test finding). Collision risk handled by content-hash
        # suffix only when truncation actually bites.
        safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip().replace(" ", "_")
        if len(safe_title) > 80:
            import hashlib as _hashlib

            digest = _hashlib.sha1(safe_title.encode()).hexdigest()[:8]
            safe_title = f"{safe_title[:70]}_{digest}"
        file_path = self._type_dir(wiki_type) / f"{safe_title}.md"

        now = time.time()
        entry = WikiEntry(
            wiki_type=wiki_type,
            title=title,
            content=content,
            file_path=str(file_path),
            tags=tags or [],
            importance=importance,
            status=status,
            created_at=now,
            updated_at=now,
        )

        md_content = self.parser.to_markdown(entry)
        await asyncio.to_thread(file_path.write_text, md_content, encoding="utf-8")

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        await self.index.save(entry, content_hash)

        # Schema lint (warning-only by default; opt-in auto_fix on the manager)
        try:
            from .lint import lint_entry, lint_missing_index, _write_index_stub

            enabled = self._get_enabled_types()
            findings = lint_entry(
                entry,
                all_titles=set(),  # no full index here; wikilinks skipped
                enabled_types=enabled,
            )
            for f in findings:
                logger.warning("wiki lint [%s] %s: %s", wiki_type, f.code, f.message)
            if self._auto_fix:
                type_dir = self._type_dir(wiki_type)
                if lint_missing_index(type_dir) is not None:
                    _write_index_stub(type_dir, wiki_type, set())
            from .secrets import scan_secrets

            for fnd in scan_secrets(content):
                logger.warning("wiki secret [%s] %s detected in %s", wiki_type, fnd.kind, file_path)
        except Exception as exc:
            logger.warning("wiki lint failed for %s: %s", file_path, exc)

        # Auto-link [[wikilinks]] to resolvable pages (non-fatal)
        try:
            await self._autolink(entry)
        except Exception as exc:
            logger.warning("wiki autolink failed for %s: %s", file_path, exc)

        # A2.3: doc → searchable sections (fail-soft)
        await self._ingest_into_rag(entry)

        return str(file_path)

    async def update(
        self,
        file_path: str,
        title: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
        status: str | None = None,
    ) -> None:
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
        if status is not None:
            entry.status = status

        entry.updated_at = time.time()

        md_content = self.parser.to_markdown(entry)
        await asyncio.to_thread(p.write_text, md_content, encoding="utf-8")

        content_hash = hashlib.sha256(entry.content.encode()).hexdigest()
        await self.index.save(entry, content_hash)

        # Auto-link any new [[wikilinks]] (non-fatal)
        try:
            await self._autolink(entry)
        except Exception as exc:
            logger.warning("wiki autolink failed for %s: %s", p, exc)

        # A2.3: doc → searchable sections (fail-soft)
        await self._ingest_into_rag(entry)

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

    async def search(self, query: str, limit: int = 10, status: str | None = "active") -> list[dict[str, Any]]:
        """Delegate search to index (A1.2: default active only, None = all)."""
        results = await self.index.search(query, limit, status=status)
        for r in results:
            # Add snippet if missing
            if "content" in r:
                r["content"] = r["content"][:500]
        return results

    async def list_by_type(self, wiki_type: str, limit: int = 20, status: str | None = "active") -> list[WikiEntry]:
        """List entries of a specific type (A1.2: default active only)."""
        rows = await self.index.list_by_type(wiki_type, limit, status=status)
        return await self._rows_to_entries(rows)

    async def list_all(self, limit: int = 50, status: str | None = "active") -> list[WikiEntry]:
        """List all entries in the layer (A1.2: default active only)."""
        rows = await self.index.list_all(limit, status=status)
        return await self._rows_to_entries(rows)

    async def get_links(self, path: str) -> list[dict[str, Any]]:
        """Return typed links involving a page path (delegates to index)."""
        return await self.index.get_links(path)

    async def _autolink(self, entry: WikiEntry) -> None:
        """Create 'follows' links for resolvable [[wikilinks]] in entry.content.

        Non-fatal: unresolvable targets are skipped; a linking error is logged.
        """
        if not entry.content or "[[" not in entry.content:
            return
        from .lint import _WIKILINK_RE

        entries = await self.list_all(limit=500)
        by_title = {e.title: e.file_path for e in entries}
        by_stem = {Path(e.file_path).stem: e.file_path for e in entries}
        for m in _WIKILINK_RE.finditer(entry.content):
            target = m.group(1).strip()
            target_path = by_title.get(target) or by_stem.get(target)
            if target_path and target_path != entry.file_path:
                await self.index.add_link(entry.file_path, target_path, "follows")

    async def _rows_to_entries(self, rows: list[dict[str, Any]]) -> list[WikiEntry]:
        entries = []
        for r in rows:
            entry = await self.get(str(r["file_path"]))
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

    async def split(self, path: str, parts: list[tuple[str, str]]) -> dict[str, Any]:
        """Split one page into several new pages from (title, content) pairs.

        The source page is left unchanged; caller can delete/retire it.
        Returns {"status","split","new_pages"} or an error dict.
        """
        if not parts:
            return {"status": "error", "message": "parts must be non-empty"}
        entry = await self.get(path)
        if entry is None:
            return {"status": "error", "message": f"no such page: {path}"}
        new_pages: list[str] = []
        for title, content in parts:
            if not title or not content:
                continue
            try:
                np = await self.add(wiki_type=entry.wiki_type, title=title, content=content)
                new_pages.append(np)
            except Exception as exc:
                logger.warning("wiki split failed for %s/%s: %s", path, title, exc)
        return {"status": "ok", "split": len(new_pages), "new_pages": new_pages}

    async def merge(self, paths: list[str], dest_title: str) -> dict[str, Any]:
        """Merge several pages into one new page (with ## source-title separators).

        Source pages are not deleted. Returns {"status","merged","dest"}.
        """
        if len(paths) < 2:
            return {"status": "error", "message": "merge needs >= 2 pages"}
        entries = [await self.get(p) for p in paths]
        if any(e is None for e in entries):
            return {"status": "error", "message": "one or more source pages missing"}
        first = entries[0]
        assert first is not None
        merged_blocks = []
        for e in entries:
            assert e is not None
            merged_blocks.append(f"## {e.title}\n\n{e.content}")
        dest = await self.add(wiki_type=first.wiki_type, title=dest_title, content="\n\n".join(merged_blocks))
        return {"status": "ok", "merged": len(entries), "dest": dest}

    async def retire(self, path: str, reason: str = "") -> dict[str, Any]:
        """Move a page to _retired/<type>/ archive and drop it from the index.

        Returns {"status","archive_path"} or {"status":"error",...}. Idempotent:
        retiring a path whose active file is already gone returns error (not crash).
        """
        try:
            p = safe_resolve(self.base_dir, path)
        except ValueError:
            return {"status": "error", "message": f"invalid path: {path}"}

        entry = await self.get(path)
        if entry is None:
            return {"status": "error", "message": f"no such page: {path}"}

        archive_dir = self.base_dir / "_retired" / entry.wiki_type
        archive_dir.mkdir(parents=True, exist_ok=True)
        import time as _t

        archive_path = archive_dir / f"{p.stem}_{int(_t.time())}.md"
        header = f"> retired: {reason}\n\n" if reason else "> retired\n\n"
        body = header + entry.content
        await asyncio.to_thread(archive_path.write_text, body, encoding="utf-8")
        if await asyncio.to_thread(p.exists):
            await asyncio.to_thread(p.unlink)
        await self.index.delete(str(p))
        return {"status": "ok", "archive_path": str(archive_path)}

    def get_enabled_types(self) -> list[str]:
        return self._get_enabled_types()

    def get_external_dirs(self) -> list[str]:
        return get_external_dirs(self.layer)

    @staticmethod
    def _promote_type_for(kind: str, layer: str) -> str:
        table = {
            ("rule", "agent"): "principle_log",
            ("rule", "user"): "work_notes",
            ("preference", "user"): "preferences",
            ("preference", "agent"): "emotional_context",
        }
        return table[(kind, layer)]

    async def promote_from_core(
        self,
        cm: AsyncConnectionManager,
        layer: str,
        user_id: str,
        min_importance: float = 0.8,
    ) -> dict[str, int]:
        """Promote high-importance rule/preference L4 facts to wiki pages.

        Deterministic, no LLM. Returns {"promoted": n, "skipped": n}.
        Idempotent: a fact whose key already has a page is skipped.
        """
        from core.memory import CoreMemory

        core = CoreMemory(cm=cm, layer=layer)
        promoted = 0
        skipped = 0
        for kind in ("rule", "preference"):
            rows = await core.list_by_kind(user_id, kind, min_importance=max(0.01, min_importance))
            wiki_type = self._promote_type_for(kind, layer)
            existing_titles = {e.title for e in await self.list_by_type(wiki_type, limit=500)}
            for r in rows:
                title = "".join(c if c.isalnum() or c in " _-" else "_" for c in str(r["key"])).strip().replace(" ", "_")
                if title in existing_titles:
                    skipped += 1
                    continue
                try:
                    await self.add(wiki_type=wiki_type, title=title, content=str(r["value"]))
                    existing_titles.add(title)
                    promoted += 1
                except Exception as exc:
                    logger.warning("wiki promote failed for %s: %s", title, exc)
                    skipped += 1
        return {"promoted": promoted, "skipped": skipped}

    async def reindex_all(self) -> dict[str, int]:
        """Re-index all .md files from disk to DB using batching and hash checks."""
        result = {"indexed": 0, "skipped": 0, "errors": 0}

        md_files = []
        enabled_types = self._get_enabled_types()

        def _collect_files() -> list[Path]:
            files: list[Path] = []
            for wiki_type in enabled_types:
                type_dir = self.base_dir / wiki_type
                if type_dir.exists() and type_dir.is_dir():
                    files.extend(list(type_dir.glob("*.md")))
            return files

        md_files = await asyncio.to_thread(_collect_files)

        async def _process_file(f: Path) -> str:
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
        """Import external .md files with concurrency control and optimization."""
        dirs = external_dirs or self.get_external_dirs()
        result = {"imported": 0, "skipped": 0, "errors": 0}
        enabled_types = self._get_enabled_types()

        # E7 least privilege: external roots are read-only mirrors. A dir that
        # resolves inside the instance data dir would let wiki import capture
        # its own data (backups, exports, DB-adjacent files) — reject it.
        from shared.connection import connection_manager as _cm

        def _resolve_roots() -> tuple[Path | None, list[Path]]:
            root = Path(str(_cm.base_dir)).resolve() if _cm.base_dir else None
            return root, [Path(d).expanduser().resolve() for d in dirs]

        data_root, resolved_dirs = await asyncio.to_thread(_resolve_roots)
        for dir_path, resolved in zip(dirs, resolved_dirs, strict=True):
            if data_root and (data_root == resolved or data_root in resolved.parents):
                raise ValueError(
                    f"external dir {resolved} is inside the data directory {data_root} — "
                    "wiki external roots are read-only mirrors, not data-dir content (E7)"
                )

        # Concurrency control: max 10 files at a time to prevent DB/FS locks
        sem = asyncio.Semaphore(10)

        for dir_path in dirs:
            p = Path(dir_path)
            if not await asyncio.to_thread(p.exists):
                continue

            md_files = await asyncio.to_thread(lambda: list(p.glob("**/*.md")))

            async def _sync_file_with_sem(f: Path) -> str:
                async with sem:
                    return await self._sync_one_file(f, enabled_types)

            tasks = [_sync_file_with_sem(f) for f in md_files]
            if tasks:
                outcomes = await asyncio.gather(*tasks)
                for o in outcomes:
                    result["errors" if o == "error" else o] += 1

        return result

    async def _sync_one_file(self, f: Path, enabled_types: list[str]) -> str:
        """Perform internal helper for single file synchronization."""
        try:
            content = await asyncio.to_thread(f.read_text, encoding="utf-8")
            parsed_entry = self.parser.parse(content, f)
            wiki_type = self._guess_type(f, parsed_entry.content)

            if wiki_type not in enabled_types:
                return "skipped"

            safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in parsed_entry.title).strip().replace(" ", "_")
            dest = self._type_dir(wiki_type) / f"{safe_title}.md"

            # Check if exists and same hash
            if await asyncio.to_thread(dest.exists):
                dest_content = await asyncio.to_thread(dest.read_text, encoding="utf-8")
                if dest_content == content:
                    return "skipped"

            await asyncio.to_thread(dest.write_text, content, encoding="utf-8")
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            parsed_entry.file_path = str(dest)
            parsed_entry.wiki_type = wiki_type
            await self.index.save(parsed_entry, content_hash)

            # Schema lint (same pattern as add())
            try:
                from .lint import lint_entry, lint_missing_index, _write_index_stub

                findings = lint_entry(
                    parsed_entry,
                    all_titles=set(),
                    enabled_types=enabled_types,
                )
                for fnd in findings:
                    logger.warning("wiki lint [%s] %s: %s", wiki_type, fnd.code, fnd.message)
                if self._auto_fix:
                    type_dir = self._type_dir(wiki_type)
                    if lint_missing_index(type_dir) is not None:
                        _write_index_stub(type_dir, wiki_type, set())
                from .secrets import scan_secrets

                for sfnd in scan_secrets(content):
                    logger.warning("wiki secret [%s] %s detected in %s", wiki_type, sfnd.kind, f)
            except Exception as exc:
                logger.warning("wiki lint failed for %s: %s", f, exc)

            return "imported"
        except Exception:
            logger.exception(f"Error syncing {f}")
            return "error"

    def _guess_type(self, path: Path, content: str) -> str:
        """Heuristically determine wiki type from path and content."""
        name_lower = path.stem.lower()
        parent_lower = path.parent.name.lower()
        content_lower = content.lower()

        # 1. Check path-based hints (O(T) where T is type count)
        all_types = LAYER_TYPES.get(self.layer, ALL_USER_TYPES)
        for t in all_types:
            if t in name_lower or t in parent_lower:
                return t

        # 2. Content-based fallback
        if self.layer == "user":
            return self._guess_user_type(content_lower)
        return self._guess_agent_type(content_lower)

    def _guess_user_type(self, content_lower: str) -> str:
        if any(w in content_lower for w in ["дневник", "diary", "сегодня"]):
            return "diary"
        if any(w in content_lower for w in ["проект", "задача"]):
            return "work_notes"
        return "diary"

    def _guess_agent_type(self, content_lower: str) -> str:
        if any(w in content_lower for w in ["решение", "decided"]):
            return "decision_log"
        if any(w in content_lower for w in ["ошибка", "error"]):
            return "error_analysis"
        return "wiki_agent"
