"""
ReadOnlyReplica — async read-only DB copy for dashboard/metrics
"""

import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ReadOnlyReplica:
    def __init__(self, source_dir: Optional[str] = None, replica_dir: Optional[str] = None):
        self.source_dir = Path(source_dir or str(Path.home() / ".mcp-ariel-memory"))
        self.replica_dir = Path(replica_dir or str(Path.home() / ".mcp-ariel-memory" / "replica"))
        self.replica_dir.mkdir(parents=True, exist_ok=True)
        self._running = False
        self._thread: threading.Thread | None = None
        self._sync_interval = 300
        self._last_sync = 0.0

    def sync(self) -> dict[str, int]:
        db_files = ["memory.db"]
        synced = {}
        for db_file in db_files:
            src = self.source_dir / db_file
            dst = self.replica_dir / db_file
            if src.exists():
                try:
                    src_conn = sqlite3.connect(str(src))
                    dst_conn = sqlite3.connect(str(dst))
                    src_conn.backup(dst_conn)
                    dst_conn.close()
                    src_conn.close()
                    synced[db_file] = 1
                except Exception as e:
                    logger.error("Replica sync failed for %s: %s" % (db_file, e))
                    try:
                        shutil.copy2(src, dst)
                        synced[db_file] = 1
                    except Exception:
                        synced[db_file] = 0
        self._last_sync = time.time()
        return synced

    def start_auto_sync(self, interval_seconds: int = 300):
        self._sync_interval = interval_seconds
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while self._running:
            try:
                if time.time() - self._last_sync >= self._sync_interval:
                    self.sync()
                time.sleep(60)
            except Exception as e:
                logger.error("Replica sync error: %s" % e)
                time.sleep(300)

    def get_conn(self, db_name: str = "memory.db") -> sqlite3.Connection:
        db_path = self.replica_dir / db_name
        if not db_path.exists():
            db_path = self.source_dir / db_name
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def is_ready(self) -> bool:
        return (self.replica_dir / "memory.db").exists()


read_only_replica = ReadOnlyReplica()
