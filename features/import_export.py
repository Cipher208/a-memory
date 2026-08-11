from __future__ import annotations

"""
Import/Export — async import/export memory between instances
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.path_safety import safe_resolve


class ImportExport:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager
        self.export_dir = self.base_dir / "exports"
        self.export_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._cm.base_dir

    async def export_user(self, user_id: str) -> str:
        core_memory: list[dict[str, Any]] = []
        episodes: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        data = {
            "user_id": user_id,
            "exported_at": time.time(),
            "version": "1.0",
            "core_memory": core_memory,
            "episodes": episodes,
            "sessions": sessions,
        }

        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("SELECT * FROM core_memory WHERE user_id=?", (user_id,))
        rows = await cursor.fetchall()
        for r in rows:
            core_memory.append({"key": r["key"], "value": r["value"], "importance": r["importance"], "created_at": r["created_at"]})

        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("SELECT * FROM episodes WHERE user_id=?", (user_id,))
        rows = await cursor.fetchall()
        for r in rows:
            episodes.append(
                {
                    "summary": r["summary"],
                    "emotional_weight": r["emotional_weight"],
                    "tags": r["tags"],
                    "created_at": r["created_at"],
                }
            )

        filename = f"export_{user_id}_{int(time.time())}.json"
        filepath = self.export_dir / filename
        await asyncio.to_thread(filepath.write_text, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(filepath)

    async def import_user(self, filepath: str, target_user_id: str | None = None) -> dict[str, int]:
        safe_resolve(self.export_dir, filepath)  # raises ValueError if traversal
        content = await asyncio.to_thread(Path(filepath).read_text, encoding="utf-8")
        data = json.loads(content)
        user_id = target_user_id or data.get("user_id", "default")
        imported = {"core_memory": 0, "episodes": 0}

        conn = await self._cm.get(DB_NAME)
        for item in data.get("core_memory", []):
            await conn.execute(
                "INSERT OR REPLACE INTO core_memory (user_id, key, value, importance, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, item["key"], item["value"], item["importance"], item["created_at"], time.time()),
            )
            imported["core_memory"] += 1
        await conn.commit()

        conn = await self._cm.get(DB_NAME)
        for item in data.get("episodes", []):
            await conn.execute(
                "INSERT INTO episodes (user_id, summary, emotional_weight, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, item["summary"], item["emotional_weight"], item["tags"], item["created_at"]),
            )
            imported["episodes"] += 1
        await conn.commit()

        return imported

    def list_exports(self) -> list[dict[str, Any]]:
        exports = []
        for f in sorted(self.export_dir.glob("export_*.json"), reverse=True):
            exports.append({"file": f.name, "size": f.stat().st_size})
        return exports
