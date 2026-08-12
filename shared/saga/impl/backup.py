import shutil
import time
import logging
import asyncio
from pathlib import Path
from typing import Any

from shared.saga.engine import SagaStep
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


async def _backup_copy_db(ctx: dict[str, Any]) -> dict[str, Any]:
    """Copy the main database file to a new backup directory."""
    base = Path.home() / ".mcp-ariel-memory"
    backup_root = base / "backups"

    def _prepare_dirs() -> Path:
        backup_root.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        b_dir = backup_root / f"backup_{timestamp}"
        b_dir.mkdir(parents=True, exist_ok=True)
        return b_dir

    backup_dir = await asyncio.to_thread(_prepare_dirs)
    src = base / DB_NAME

    def _copy() -> bool:
        if src.exists():
            dest = backup_dir / DB_NAME
            shutil.copy2(src, dest)
            return True
        return False

    if await asyncio.to_thread(_copy):
        logger.info(f"Database backed up to {backup_dir / DB_NAME}")
        ctx["backup_path"] = str(backup_dir)
        return {"backup_path": str(backup_dir), "status": "copied"}

    logger.warning(f"Source database {src} not found for backup")
    return {"status": "skipped_no_source"}


async def _backup_verify(ctx: dict[str, Any]) -> dict[str, Any]:
    """Verify that the backup file exists and is not empty."""
    backup_path_str = ctx.get("backup_path")
    if not backup_path_str:
        return {"verified": False, "reason": "no_backup_path"}

    backup_path = Path(backup_path_str)
    db_file = backup_path / DB_NAME

    def _check() -> tuple[bool, int]:
        if db_file.exists():
            size = db_file.stat().st_size
            return size > 0, int(size)
        return False, 0

    exists, size = await asyncio.to_thread(_check)
    if exists:
        return {"verified": True, "size": size}

    return {"verified": False, "reason": "file_missing_or_empty"}


async def _backup_compensate(ctx: dict[str, Any]) -> None:
    """Delete the backup directory if the saga fails."""
    backup_path_str = ctx.get("backup_path")
    if backup_path_str:
        backup_path = Path(backup_path_str)

        def _cleanup() -> None:
            if backup_path.exists():
                shutil.rmtree(backup_path)

        await asyncio.to_thread(_cleanup)
        logger.info(f"Backup directory {backup_path} removed during compensation")


def create_backup_saga() -> list[SagaStep]:
    """
    Returns steps for the database backup saga.
    Logic: Copy DB file -> Verify existence.
    Compensation: Delete backup dir on failure.
    """
    return [
        SagaStep(name="copy_db", action=_backup_copy_db, compensation=_backup_compensate, retry_attempts=2),
        SagaStep(name="verify_backup", action=_backup_verify),
    ]
