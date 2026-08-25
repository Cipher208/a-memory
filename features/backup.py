from __future__ import annotations

"""
Backup — async backup/restore of all databases
"""

import contextlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from config import config
from shared.constants import BACKUP_DIR_NAME, MANIFEST_FILE, UTF8, DB_NAME
from shared.path_safety import safe_resolve

logger = logging.getLogger(__name__)


def snapshot_sqlite(src: Path, dest: Path) -> None:
    """Consistent copy of a live SQLite DB via the online backup API.

    shutil.copy2 on a WAL-mode database can capture torn pages; the
    backup API copies a transactionally consistent snapshot instead.
    """
    import sqlite3

    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


class BackupManager:
    def __init__(self, base_dir: str | None = None):
        self.base_dir = Path(base_dir or str(Path.home() / ".mcp-ariel-memory"))
        self.backup_dir = self.base_dir / BACKUP_DIR_NAME
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    async def backup(self, label: str | None = None) -> str:
        import uuid

        timestamp = int(time.time())
        name = label or f"backup_{timestamp}_{uuid.uuid4().hex[:6]}"
        dest = self.backup_dir / name
        dest.mkdir(parents=True, exist_ok=True)

        db_files = [DB_NAME]
        backed_up = []
        for db_file in db_files:
            src = self.base_dir / db_file
            if src.exists() and not src.is_symlink():
                snapshot_sqlite(src, dest / db_file)
                backed_up.append(db_file)

        manifest = {"name": name, "timestamp": timestamp, "files": backed_up}
        (dest / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2), encoding=UTF8)
        return str(dest)

    async def restore(self, backup_name: str) -> dict[str, Any]:
        src = self.backup_dir / backup_name
        if not src.exists() or src.is_symlink():
            return {"error": f"Backup not found or invalid: {backup_name}"}

        manifest_path = src / MANIFEST_FILE
        if manifest_path.exists() and not manifest_path.is_symlink():
            manifest = json.loads(manifest_path.read_text(encoding=UTF8))
        else:
            manifest = {"files": [f.name for f in src.glob("*.db")]}

        restored = []
        for db_file in manifest.get("files", []):
            # Whitelisted filenames are safe, plus safe_resolve guard.
            dest_path = self.base_dir / db_file
            if dest_path.exists() and dest_path.is_symlink():
                logger.error("Refusing to restore over symlink: %s", dest_path)
                continue

            safe_resolve(self.base_dir, db_file)  # raises ValueError if traversal
            backup_file = src / db_file
            if backup_file.exists() and not backup_file.is_symlink():
                # skylos: ignore [SKY-D215, SKY-D325] - Verified safe via safe_resolve and symlink checks
                shutil.copy2(backup_file, dest_path)
                restored.append(db_file)

        return {"restored": restored, "backup": backup_name}

    def list_backups(self) -> list[dict[str, Any]]:
        backups = []
        if not self.backup_dir.exists():
            return []
        for d in sorted(self.backup_dir.iterdir(), reverse=True):
            if d.is_dir() and not d.is_symlink():
                info = {"name": d.name}
                manifest_path = d / MANIFEST_FILE
                if manifest_path.exists() and not manifest_path.is_symlink():
                    with contextlib.suppress(Exception):
                        info.update(json.loads(manifest_path.read_text(encoding=UTF8)))
                backups.append(info)
        return backups

    def cleanup_old(self) -> int:
        cutoff = time.time() - (config.get("backup", "backup_retention_days") or 30) * 86400
        removed = 0
        if not self.backup_dir.exists():
            return 0
        for d in self.backup_dir.iterdir():
            if d.is_dir() and not d.is_symlink() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
                removed += 1
        return removed
