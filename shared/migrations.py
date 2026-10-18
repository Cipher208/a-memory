from __future__ import annotations

"""
DB Migrations — async, unified memory.db using Alembic
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

import alembic.command as alembic_command
from shared.connection import AsyncConnectionManager, connection_manager
from shared.write_dispatch import get_write_dispatch

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

        # Dynamically set the database URL to point to our current DB
        db_path = self._cm.base_dir / DB_NAME
        # Alembic expects a standard sqlite:/// path
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

        return cfg

    async def get_current_version(self) -> str | None:
        try:
            row = await get_write_dispatch(self._cm).select_one(DB_NAME, "SELECT version_num FROM alembic_version")
            return str(row["version_num"]) if row else None
        except Exception:
            return None

    async def get_head_version(self) -> str | None:
        """Latest revision in the alembic script directory; None if unavailable."""

        def _head() -> str | None:
            from alembic.script import ScriptDirectory

            return ScriptDirectory.from_config(self._get_alembic_config()).get_current_head()

        try:
            return await asyncio.to_thread(_head)
        except Exception:
            return None

    async def migrate(self) -> dict[str, Any]:
        """Run pending migrations using Alembic.

        Alembic is invoked only when the base is actually behind.

        Calling `upgrade(cfg, "head")` unconditionally meant every process start
        opened a write transaction just to discover there was nothing to apply —
        and `lifespan` runs this **before** the server answers anything, on every
        one of the day's eleven starts (measured 14.10.2026). With several
        processes writing one SQLite file, that transaction could not get the
        write lock, the client's `connect_timeout` expired first, and the
        session came up without memory tools:

            Failed to connect to MCP server 'ariel-memory': CancelledError

        Reading the version and comparing it against the script heads needs no
        database connection, so the steady state now costs nothing and cannot
        queue behind another writer.
        """
        current = await self.get_current_version()

        # Run Alembic upgrade in a thread to avoid blocking async loop
        # (Alembic/SQLAlchemy sync nature)
        def run_upgrade() -> None:
            cfg = self._get_alembic_config()
            heads = set(ScriptDirectory.from_config(cfg).get_heads())
            if current and current in heads:
                logger.info("Migrations: already at %s, nothing to apply", current)
                return
            alembic_command.upgrade(cfg, "head")

        logger.info("Starting DB migration via Alembic...")
        await asyncio.to_thread(run_upgrade)

        new_version = await self.get_current_version()

        return {"current_version": current, "new_version": new_version, "status": "up_to_date" if new_version else "initialized"}

    async def get_pending(self) -> list[str]:
        # Simple check: if current != head
        return ["Update to head"]


migration_manager = MigrationManager()
