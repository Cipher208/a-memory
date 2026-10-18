from __future__ import annotations

"""
AuditTrail — async, SQLite-based audit logging with rotation
"""

import asyncio
import json
import logging
import time
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch, get_write_dispatch

logger = logging.getLogger(__name__)


class AuditTrail:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    @property
    def _dispatch(self) -> WriteDispatch:
        """Resolved on every access so a repointed ``_cm`` is honoured."""
        return get_write_dispatch(self._cm)

    async def _init_db(self) -> None:
        await self._dispatch.execute_script(
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
        await self._dispatch.write(
            DB_NAME,
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, action, layer, target_id, json.dumps(details or {}), time.time()),
        )

    async def log_verify(self, user_id: str, verified: int, dropped: int) -> None:
        """E5: record D1.5 verification aggregate per recall (feeds integrity score)."""
        await self.log(user_id, "verify_result", layer="user", details={"verified": verified, "dropped": dropped})

    async def get_history(self, user_id: str, limit: int = 50, action: str | None = None) -> list[dict[str, Any]]:
        if action:
            rows = await self._dispatch.query(
                DB_NAME,
                "SELECT * FROM audit_log WHERE user_id=? AND action=? ORDER BY timestamp DESC LIMIT ?",
                (user_id, action, limit),
            )
        else:
            rows = await self._dispatch.query(
                DB_NAME,
                "SELECT * FROM audit_log WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
                (user_id, limit),
            )
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
        if user_id:
            value = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM audit_log WHERE user_id=?", (user_id,))
        else:
            value = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM audit_log")
        return int(value) if value is not None else 0

    async def cleanup_old(self, retention_days: int = 30) -> int:
        cutoff = time.time() - (retention_days * 86400)
        shared = await self._cm.get(DB_NAME)
        if shared.in_transaction:
            # Retention is best-effort: never ride or flush a foreign transaction.
            logger.warning("audit cleanup_old skipped: shared connection holds a transaction")
            return 0
        return int(await self._dispatch.write(DB_NAME, "DELETE FROM audit_log WHERE timestamp < ?", (cutoff,)))

    async def archive_and_prune(self, retention_days: int = 30, archive_dir: str | None = None) -> dict[str, int]:
        cutoff = time.time() - (retention_days * 86400)
        rows = await self._dispatch.query(
            DB_NAME,
            "SELECT * FROM audit_log WHERE timestamp < ? ORDER BY timestamp",
            (cutoff,),
        )

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

        pruned = await self._dispatch.write(DB_NAME, "DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
        return {"archived": len(rows), "pruned": pruned}

    async def count_all(self) -> int:
        value = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM audit_log")
        return int(value) if value is not None else 0
