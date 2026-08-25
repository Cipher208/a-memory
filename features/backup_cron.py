from __future__ import annotations

"""
Backup Cron — automatic scheduled backups with jitter + wiki sync.
"""

import asyncio
import contextlib
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

from config import config
from features.backup import snapshot_sqlite
from shared.path_safety import safe_resolve

logger = logging.getLogger(__name__)


class BackupCron:
    def __init__(self, base_dir: str | None = None):
        self.base_dir = Path(base_dir or str(Path.home() / ".mcp-ariel-memory"))
        self.backup_dir = self.base_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.interval_hours = config.get("backup", "backup_interval_hours") or 24
        self.retention_days = config.get("backup", "backup_retention_days") or 30
        self.jitter_seconds = config.get("backup", "jitter_seconds") or 3600
        self.wiki_sync_interval = config.get("backup", "wiki_sync_interval_minutes") or 30
        self._running = False
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._last_backup = 0.0
        self._last_wiki_sync = 0.0
        self._state_file = self.base_dir / ".backup_cron_state.json"
        self._load_state()

    def _load_state(self) -> None:
        if self._state_file.exists():
            with contextlib.suppress(Exception):
                from shared.saga import read_state_legacy_or_encrypted

                state = read_state_legacy_or_encrypted(self._state_file)
                self._last_backup = state.get("last_backup", 0.0)
                self._last_wiki_sync = state.get("last_wiki_sync", 0.0)

    def _save_state(self) -> None:
        self._state_file.write_text(json.dumps({"last_backup": self._last_backup, "last_wiki_sync": self._last_wiki_sync}), encoding="utf-8")

    def start(self) -> None:
        if self._running:
            return
        from config import config

        if not config.is_feature_enabled("backup_cron"):
            return
        if os.environ.get("BACKUP_CRON_DISABLED"):
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        jitter_info = f" (+{self.jitter_seconds}s jitter)" if self.jitter_seconds else ""
        logger.info(f"Backup cron started (interval={self.interval_hours}h{jitter_info})")

    def capture_main_loop(self) -> None:
        """Remember the server's event loop for cron-thread jobs.

        aiosqlite connections are bound to that loop; scheduling coroutines
        onto it avoids the cross-loop deadlock class.
        """
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None

    def _await_on_main_loop(self, coro: Any, timeout: float = 120) -> Any:
        if self._main_loop is not None and self._main_loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, self._main_loop).result(timeout=timeout)
        return asyncio.run(coro)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
                time.sleep(60)
            except Exception:
                logger.exception("Backup cron error")
                time.sleep(300)

    def _tick(self) -> None:
        now = time.time()
        self._check_backup(now)
        self._check_wiki_sync(now)

    def _check_backup(self, now: float) -> None:
        next_backup = self._last_backup + self.interval_hours * 3600
        if now < next_backup:
            return

        jitter = random.randint(0, self.jitter_seconds) if self.jitter_seconds else 0
        if jitter:
            logger.info(f"Backup jitter: waiting {jitter}s")
            time.sleep(jitter)

        self._do_backup()
        self._cleanup_old()
        self._fire_nightly_hooks()

    def _check_wiki_sync(self, now: float) -> None:
        if now - self._last_wiki_sync >= self.wiki_sync_interval * 60:
            self._sync_wiki()

    def _fire_nightly_hooks(self) -> None:
        """Trigger nightly maintenance hooks for both layers."""
        try:
            from hooks.registry import hook_registry

            for layer in ["user", "agent"]:
                self._await_on_main_loop(hook_registry.fire("nightly", layer, {"trigger": "backup_cron"}))
        except Exception:
            logger.exception("Nightly hook error")

    def _do_backup(self) -> str:
        import shutil
        import uuid

        timestamp = int(time.time())
        name = f"auto_{timestamp}_{uuid.uuid4().hex[:6]}"
        dest = self.backup_dir / name
        dest.mkdir(parents=True, exist_ok=True)

        db_files = ["memory.db"]
        backed_up = []
        for db_file in db_files:
            src = self.base_dir / db_file
            if src.exists():
                snapshot_sqlite(src, dest / db_file)
                backed_up.append(db_file)

        # Backup wiki .md files
        wiki_dir = self.base_dir / "wiki"
        if wiki_dir.exists():
            shutil.copytree(wiki_dir, dest / "wiki", dirs_exist_ok=True)
            backed_up.append("wiki/")

        manifest = {"name": name, "timestamp": timestamp, "files": backed_up}
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        self._last_backup = time.time()
        self._save_state()
        logger.info("Auto-backup created: %s (%d files)", name, len(backed_up))
        return str(dest)

    def _cleanup_old(self) -> None:
        import shutil

        cutoff = time.time() - (self.retention_days * 86400)
        removed = 0
        for d in self.backup_dir.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
                removed += 1
        if removed:
            logger.info("Cleaned up %d old backups", removed)

    def _sync_wiki(self) -> None:
        """Synchronize wiki files with disk."""
        try:
            from wiki import WikiManager

            for layer in ["user", "agent"]:
                fw = WikiManager(layer=layer)
                raw = fw.reindex_all()
                result: dict[str, Any] = self._await_on_main_loop(raw) if asyncio.iscoroutine(raw) else raw
                if isinstance(result, dict) and result.get("indexed", 0) > 0:
                    logger.info("Wiki %s synced: %d files", layer, result["indexed"])
            self._last_wiki_sync = time.time()
            self._save_state()
        except Exception:
            logger.exception("Wiki sync error")

    def backup_now(self) -> str:
        return self._do_backup()

    def restore(self, backup_name: str) -> dict[str, Any]:
        import shutil

        src = self.backup_dir / backup_name
        if not src.exists() or src.is_symlink():
            return {"error": f"Backup not found or invalid: {backup_name}"}

        manifest_path = src / "manifest.json"
        if manifest_path.exists() and not manifest_path.is_symlink():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {"files": [f.name for f in src.glob("*.db")]}

        restored = []
        for db_file in manifest.get("files", []):
            dest_path = self.base_dir / db_file
            if dest_path.exists() and dest_path.is_symlink():
                logger.error("Refusing to restore over symlink: %s", dest_path)
                continue

            safe_resolve(self.base_dir, db_file)  # raises ValueError if traversal
            if db_file.endswith("/"):
                # Restore wiki directory
                src_wiki = src / db_file
                if src_wiki.exists() and not src_wiki.is_symlink():
                    shutil.copytree(src_wiki, dest_path, dirs_exist_ok=True)
                    restored.append(db_file)
            else:
                backup_file = src / db_file
                if backup_file.exists() and not backup_file.is_symlink():
                    # skylos: ignore [SKY-D215, SKY-D325] - Safe via safe_resolve and symlink checks
                    shutil.copy2(backup_file, dest_path)
                    restored.append(db_file)

        return {"restored": restored, "backup": backup_name}

    def list_backups(self) -> list[dict[str, Any]]:
        backups: list[dict[str, Any]] = []
        for d in sorted(self.backup_dir.iterdir(), reverse=True):
            if d.is_dir():
                info = {"name": d.name}
                manifest_path = d / "manifest.json"
                if manifest_path.exists():
                    with contextlib.suppress(Exception):
                        info.update(json.loads(manifest_path.read_text(encoding="utf-8")))
                backups.append(info)
        return backups

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "interval_hours": self.interval_hours,
            "jitter_seconds": self.jitter_seconds,
            "retention_days": self.retention_days,
            "wiki_sync_interval_minutes": self.wiki_sync_interval,
            "last_backup": self._last_backup,
            "next_backup": self._last_backup + self.interval_hours * 3600,
            "backup_count": len(list(self.backup_dir.iterdir())),
        }


backup_cron = BackupCron()
