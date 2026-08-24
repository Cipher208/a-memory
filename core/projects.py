from __future__ import annotations

"""
ProjectMemory — persistent layer about software/other projects.

SQLite holds structured data and indexes (identity, decisions, artifact
map, code-symbol index). Large documents live in the Wiki; SQLite keeps
only references. One DB file per instance: projects.db.
"""

import time
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager

PROJECTS_DB = "projects.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pdecisions_project ON project_decisions(project_name, created_at);
CREATE TABLE IF NOT EXISTS project_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    path TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    content_hash TEXT,
    mtime REAL,
    wiki_ref TEXT,
    updated_at REAL NOT NULL,
    UNIQUE(project_name, path)
);
CREATE TABLE IF NOT EXISTS project_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    line INTEGER,
    UNIQUE(project_name, symbol, source_file, line)
);
CREATE INDEX IF NOT EXISTS idx_psymbols_project ON project_symbols(project_name);
"""


class ProjectMemory:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def _init_db(self) -> None:
        await self._cm.execute_script(PROJECTS_DB, _SCHEMA)

    # ── identity ──

    async def upsert_project(self, name: str, summary: str = "", status: str = "", path: str = "") -> None:
        conn = await self._cm.get(PROJECTS_DB)
        now = time.time()
        await conn.execute(
            """INSERT INTO projects (name, status, summary, path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 status = CASE WHEN ? != '' THEN ? ELSE status END,
                 summary = CASE WHEN ? != '' THEN ? ELSE summary END,
                 path = CASE WHEN ? != '' THEN ? ELSE path END,
                 updated_at = ?""",
            (
                name,
                status or "active",
                summary,
                path,
                now,
                now,
                status,
                status,
                summary,
                summary,
                path,
                path,
                now,
            ),
        )
        await conn.commit()

    async def get_project(self, name: str) -> dict[str, Any] | None:
        conn = await self._cm.get(PROJECTS_DB)
        cursor = await conn.execute("SELECT * FROM projects WHERE name=?", (name,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ── decisions ──

    async def add_decision(self, project_name: str, decision: str, rationale: str = "", outcome: str = "") -> int:
        conn = await self._cm.get(PROJECTS_DB)
        cursor = await conn.execute(
            "INSERT INTO project_decisions (project_name, decision, rationale, outcome, created_at) VALUES (?, ?, ?, ?, ?)",
            (project_name, decision, rationale, outcome, time.time()),
        )
        await conn.commit()
        await self.touch(project_name)
        return int(cursor.lastrowid or 0)

    async def list_decisions(self, project_name: str, limit: int = 20) -> list[dict[str, Any]]:
        conn = await self._cm.get(PROJECTS_DB)
        cursor = await conn.execute(
            "SELECT decision, rationale, outcome, created_at FROM project_decisions WHERE project_name=? ORDER BY created_at DESC LIMIT ?",
            (project_name, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── artifacts ──

    async def upsert_artifact(
        self,
        project_name: str,
        path: str,
        role: str = "",
        status: str = "",
        content_hash: str | None = None,
        mtime: float | None = None,
        wiki_ref: str | None = None,
    ) -> None:
        conn = await self._cm.get(PROJECTS_DB)
        await conn.execute(
            """INSERT INTO project_artifacts (project_name, path, role, status, content_hash, mtime, wiki_ref, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_name, path) DO UPDATE SET
                 role = excluded.role, status = excluded.status,
                 content_hash = COALESCE(excluded.content_hash, project_artifacts.content_hash),
                 mtime = COALESCE(excluded.mtime, project_artifacts.mtime),
                 wiki_ref = COALESCE(excluded.wiki_ref, project_artifacts.wiki_ref),
                 updated_at = excluded.updated_at""",
            (project_name, path, role, status, content_hash, mtime, wiki_ref, time.time()),
        )
        await conn.commit()
        await self.touch(project_name)

    async def list_artifacts(self, project_name: str, limit: int = 100) -> list[dict[str, Any]]:
        conn = await self._cm.get(PROJECTS_DB)
        cursor = await conn.execute(
            "SELECT path, role, status, content_hash, mtime, wiki_ref, updated_at FROM project_artifacts WHERE project_name=? ORDER BY updated_at DESC LIMIT ?",
            (project_name, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── code symbol index ──

    async def replace_symbols(self, project_name: str, symbols: list[dict[str, Any]]) -> int:
        """Bulk-replace the code index for a project (from graphify graph.json)."""
        conn = await self._cm.get(PROJECTS_DB)
        await conn.execute("DELETE FROM project_symbols WHERE project_name=?", (project_name,))
        rows = [
            (
                project_name,
                str(s.get("label", s.get("id", "")))[:300],
                str(s.get("file_type", "")),
                str(s.get("source_file", "")),
                int(str(s.get("source_location", "L0"))[1:] or 0),
            )
            for s in symbols[:5000]
        ]
        if rows:
            await conn.executemany(
                "INSERT OR IGNORE INTO project_symbols (project_name, symbol, kind, source_file, line) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        await conn.commit()
        await self.touch(project_name)
        return len(rows)

    async def count_symbols(self, project_name: str) -> int:
        conn = await self._cm.get(PROJECTS_DB)
        cursor = await conn.execute("SELECT COUNT(*) FROM project_symbols WHERE project_name=?", (project_name,))
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def touch(self, name: str) -> None:
        conn = await self._cm.get(PROJECTS_DB)
        await conn.execute("UPDATE projects SET updated_at=? WHERE name=?", (time.time(), name))
        await conn.commit()
