from __future__ import annotations
"""
DB Migrations — async, unified memory.db using Alembic
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from alembic.config import Config as AlembicConfig

import alembic.command as alembic_command
from shared.connection import AsyncConnectionManager, connection_manager

logger = logging.getLogger(__name__)

DB_NAME = "memory.db"


class MigrationManager:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager
        self._repo_root = Path(__file__).parent.parent
        self._alembic_ini = self._repo_root / "alembic.ini"

    def _get_alembic_config(self) -> AlembicConfig:
        cfg = AlembicConfig(str(self._alembic_ini))
        # Ensure alembic uses the correct directory for versions
        cfg.set_main_option("script_location", str(self._repo_root / "alembic"))
        return cfg

    async def get_current_version(self) -> str | None:
        conn = await self._cm.get(DB_NAME)
        try:
            row = await (await conn.execute("SELECT version_num FROM alembic_version")).fetchone()
            return row["version_num"] if row else None
        except Exception:
            return None

    async def migrate(self) -> dict[str, Any]:
        """Run all pending migrations using Alembic."""
        current = await self.get_current_version()

        # Run Alembic upgrade in a thread to avoid blocking async loop
        # (Alembic/SQLAlchemy sync nature)
        def run_upgrade():
            cfg = self._get_alembic_config()
            alembic_command.upgrade(cfg, "head")

        logger.info("Starting DB migration via Alembic...")
        await asyncio.to_thread(run_upgrade)

        new_version = await self.get_current_version()

        return {"current_version": current, "new_version": new_version, "status": "up_to_date" if new_version else "initialized"}

    async def get_pending(self) -> list[str]:
        # Simple check: if current != head
        return ["Update to head"]


migration_manager = MigrationManager()
