from __future__ import annotations

"""
AuditTrail — async, SQLite-based audit logging with rotation
"""

import asyncio
import json
import time
from typing import Any, cast

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


class AuditTrail:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def _init_db(self) -> None:
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                layer TEXT,
                target_id TEXT,
                details TEXT,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_user_action ON audit_log(user_id, action, timestamp DESC);
        """,
        )

    async def log(
        self,
        user_id: str,
        action: str,
        layer: str | None = None,
        target_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        from config import config

        if not config.is_feature_enabled("audit_trail"):
            return
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, action, layer, target_id, json.dumps(details or {}), time.time()),
        )
        await conn.commit()

    async def log_verify(self, user_id: str, verified: int, dropped: int) -> None:
        """E5: record D1.5 verification aggregate per recall (feeds integrity score)."""
        await self.log(user_id, "verify_result", layer="user", details={"verified": verified, "dropped": dropped})

    async def get_history(self, user_id: str, limit: int = 50, action: str | None = None) -> list[dict[str, Any]]:
        conn = await self._cm.get(DB_NAME)
        if action:
            cursor = await conn.execute(
                "SELECT * FROM audit_log WHERE user_id=? AND action=? ORDER BY timestamp DESC LIMIT ?",
                (user_id, action, limit),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM audit_log WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
                (user_id, limit),
            )
        rows = await cursor.fetchall()
        return [
            {
                "log_id": r["log_id"],
                "action": r["action"],
                "layer": r["layer"],
                "target_id": r["target_id"],
                "details": json.loads(r["details"]) if r["details"] else {},
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    async def count(self, user_id: str | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if user_id:
            cursor = await conn.execute("SELECT COUNT(*) FROM audit_log WHERE user_id=?", (user_id,))
        else:
            cursor = await conn.execute("SELECT COUNT(*) FROM audit_log")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def cleanup_old(self, retention_days: int = 30) -> int:
        cutoff = time.time() - (retention_days * 86400)
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
        await conn.commit()
        return cast("int", cursor.rowcount)

    async def archive_and_prune(self, retention_days: int = 30, archive_dir: str | None = None) -> dict[str, int]:
        cutoff = time.time() - (retention_days * 86400)
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT * FROM audit_log WHERE timestamp < ? ORDER BY timestamp",
            (cutoff,),
        )
        rows = await cursor.fetchall()

        if not rows:
            return {"archived": 0, "pruned": 0}

        if archive_dir:
            from pathlib import Path

            archive_path = Path(archive_dir)
            await asyncio.to_thread(archive_path.mkdir, parents=True, exist_ok=True)
            archive_file = archive_path / f"audit_archive_{int(time.time())}.json"
            archive_data = [
                {
                    "log_id": r["log_id"],
                    "user_id": r["user_id"],
                    "action": r["action"],
                    "details": json.loads(r["details"]) if r["details"] else {},
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]
            await asyncio.to_thread(archive_file.write_text, json.dumps(archive_data, indent=2, default=str), encoding="utf-8")

        cursor = await conn.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
        await conn.commit()
        return {"archived": len(rows), "pruned": cursor.rowcount}

    async def count_all(self) -> int:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("SELECT COUNT(*) FROM audit_log")
        row = await cursor.fetchone()
        return row[0] if row else 0
