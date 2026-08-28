from __future__ import annotations

"""
Import/Export — async import/export memory between instances
"""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.path_safety import safe_resolve

# user_id lands in filesystem names on export — keep it strict.
_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ImportExport:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager
        self.export_dir = self.base_dir / "exports"
        self.export_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        res: Any = self._cm.base_dir
        return Path(res) if res else Path.home() / ".mcp-ariel-memory"

    async def export_user(self, user_id: str) -> str:
        if not _USER_ID_RE.match(user_id):
            raise ValueError(f"user_id may contain only [A-Za-z0-9._-], got: {user_id!r}")

        core_memory: list[dict[str, Any]] = []
        episodes: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        data = {
            "user_id": user_id,
            "exported_at": time.time(),
            "version": "1.1",
            "core_memory": core_memory,
            "episodes": episodes,
            "sessions": sessions,
        }

        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("SELECT * FROM core_memory WHERE user_id=?", (user_id,))
        rows = await cursor.fetchall()
        for r in rows:
            core_memory.append(
                {
                    "layer": r["layer"],
                    "key": r["key"],
                    "value": r["value"],
                    "importance": r["importance"],
                    "memory_kind": r["memory_kind"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )

        cursor = await conn.execute("SELECT * FROM episodes WHERE user_id=?", (user_id,))
        rows = await cursor.fetchall()
        for r in rows:
            episodes.append(
                {
                    "layer": r["layer"],
                    "summary": r["summary"],
                    "emotional_weight": r["emotional_weight"],
                    "tags": r["tags"],
                    "created_at": r["created_at"],
                }
            )

        cursor = await conn.execute("SELECT * FROM sessions WHERE user_id=?", (user_id,))
        for r in await cursor.fetchall():
            sessions.append(
                {
                    "session_id": r["session_id"],
                    "summary": r["summary"],
                    "state_deltas": r["state_deltas"],
                    "topics": r["topics"],
                    "message_count": r["message_count"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                }
            )

        filename = f"export_{user_id}_{int(time.time())}.json"
        filepath = self.export_dir / filename
        await asyncio.to_thread(filepath.write_text, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(filepath)

    async def import_user(self, filepath: str, target_user_id: str | None = None) -> dict[str, int]:
        resolved = safe_resolve(self.export_dir, filepath)  # raises ValueError if traversal
        content = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
        data = json.loads(content)
        user_id = target_user_id or data.get("user_id", "default")
        imported = {"core_memory": 0, "episodes": 0, "sessions": 0}

        conn = await self._cm.get(DB_NAME)
        try:
            # Newest-wins conflict guard: a stale backup must never clobber
            # newer live values. Layer travels with the row.
            core_rows = [
                (
                    item.get("layer", "user"),
                    user_id,
                    item["key"],
                    item["value"],
                    item["importance"],
                    item.get("memory_kind"),
                    item["created_at"],
                    item.get("updated_at") or time.time(),
                )
                for item in data.get("core_memory", [])
            ]
            if core_rows:
                await conn.executemany(
                    """INSERT INTO core_memory (layer, user_id, key, value, importance, memory_kind, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(layer, user_id, key) DO UPDATE SET
                           value=excluded.value,
                           importance=excluded.importance,
                           memory_kind=excluded.memory_kind,
                           updated_at=excluded.updated_at
                       WHERE excluded.updated_at > core_memory.updated_at""",
                    core_rows,
                )
            imported["core_memory"] = len(core_rows)

            episode_rows = [
                (
                    item.get("layer", "user"),
                    user_id,
                    item["summary"],
                    item["emotional_weight"],
                    item["tags"],
                    item["created_at"],
                )
                for item in data.get("episodes", [])
            ]
            if episode_rows:
                await conn.executemany(
                    "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    episode_rows,
                )
            imported["episodes"] = len(episode_rows)

            session_rows = [
                (
                    item["session_id"],
                    user_id,
                    item.get("summary"),
                    item.get("state_deltas"),
                    item.get("topics"),
                    item.get("message_count", 0),
                    item.get("started_at"),
                    item.get("ended_at"),
                )
                for item in data.get("sessions", [])
            ]
            if session_rows:
                await conn.executemany(
                    """INSERT OR IGNORE INTO sessions (session_id, user_id, summary, state_deltas, topics, message_count, started_at, ended_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    session_rows,
                )
            imported["sessions"] = len(session_rows)

            await conn.commit()
        except Exception:
            # Half-applied imports are worse than failed ones
            await conn.rollback()
            raise

        return imported

    def list_exports(self, user_id: str | None = None) -> list[dict[str, Any]]:
        pattern = f"export_{user_id}_*.json" if user_id else "export_*.json"
        exports = []
        for f in sorted(self.export_dir.glob(pattern), reverse=True):
            exports.append({"file": f.name, "size": f.stat().st_size})
        return exports
