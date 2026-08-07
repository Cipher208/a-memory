"""
Backup — async backup/restore of all databases
"""

import json
import shutil
import time
from pathlib import Path
from typing import Any

from config import config
from shared.path_safety import safe_resolve
import contextlib


class BackupManager:
    def __init__(self, base_dir: str | None = None):
        self.base_dir = Path(base_dir or str(Path.home() / ".mcp-ariel-memory"))
        self.backup_dir = self.base_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    async def backup(self, label: str | None = None) -> str:
        import uuid

        timestamp = int(time.time())
        name = label or f"backup_{timestamp}_{uuid.uuid4().hex[:6]}"
        dest = self.backup_dir / name
        dest.mkdir(parents=True, exist_ok=True)

        db_files = ["memory.db"]
        backed_up = []
        for db_file in db_files:
            src = self.base_dir / db_file
            if src.exists():
                shutil.copy2(src, dest / db_file)
                backed_up.append(db_file)

        manifest = {"name": name, "timestamp": timestamp, "files": backed_up}
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return str(dest)

    async def restore(self, backup_name: str) -> dict[str, Any]:
        src = self.backup_dir / backup_name
        if not src.exists():
            return {"error": f"Backup not found: {backup_name}"}

        manifest_path = src / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"files": [f.name for f in src.glob("*.db")]}

        restored = []
        for db_file in manifest.get("files", []):
            safe_resolve(self.base_dir, db_file)  # raises ValueError if traversal
            backup_file = src / db_file
            if backup_file.exists():
                shutil.copy2(backup_file, self.base_dir / db_file)
                restored.append(db_file)

        return {"restored": restored, "backup": backup_name}

    def list_backups(self) -> list[dict[str, Any]]:
        backups = []
        for d in sorted(self.backup_dir.iterdir(), reverse=True):
            if d.is_dir():
                info = {"name": d.name}
                manifest_path = d / "manifest.json"
                if manifest_path.exists():
                    with contextlib.suppress(Exception):
                        info.update(json.loads(manifest_path.read_text(encoding="utf-8")))
                backups.append(info)
        return backups

    def cleanup_old(self) -> int:
        import shutil as sh

        cutoff = time.time() - (config.get("backup", "backup_retention_days") or 30) * 86400
        removed = 0
        for d in self.backup_dir.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                sh.rmtree(d)
                removed += 1
        return removed
