"""SQLite Connection Manager — Thread-safe, platform-aware async I/O.

Ensures singleton connections per database file with optimized
concurrency settings (WAL mode, busy_timeout) for VPS environments.
"""

from __future__ import annotations

"""
AsyncConnectionManager — unified SQLite connection manager.

Rules:
- One connection per DB file (no pool — connection pooling is an anti-pattern for SQLite)
- WAL + busy_timeout for concurrency
- Platform-aware: aiosqlite on Linux/macOS, sync sqlite3 + to_thread on Windows
- row_factory = sqlite3.Row (compatible across all platforms)

Usage:
    cm = AsyncConnectionManager()
    conn = await cm.get("memory.db")
    cur = await conn.execute("SELECT * FROM users WHERE id=?", (uid,))
    row = await cur.fetchone()
"""

import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _wal_enabled() -> bool:
    # Imported lazily: config.py sits at repo root, shared/ must stay import-light.
    from config import config

    return bool(config.get("performance", "wal_mode", default=True))


_DEFAULT_DIR = os.environ.get(
    "MCP_MEMORY_DATA_DIR",
    str(Path.home() / ".mcp-ariel-memory"),
)

# Windows has aiosqlite threading bug — use sync sqlite3 + to_thread
# On Linux/macOS, try aiosqlite first, fallback to sync if not installed
_USE_SYNC = sys.platform == "win32"
_HAS_AIOSQLITE = False

if not _USE_SYNC:
    import importlib.util

    if importlib.util.find_spec("aiosqlite") is not None:
        _HAS_AIOSQLITE = True
    else:
        _USE_SYNC = True
        logger.warning("aiosqlite not installed, falling back to sync sqlite3")


class _SyncConnectionWrapper:
    """Wrap sync sqlite3.Connection to look like aiosqlite for Windows fallback."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.row_factory = conn.row_factory
        self._lock = threading.Lock()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _SyncCursorWrapper:
        # sql is validated by caller, params are handled safely by sqlite3
        def _do() -> sqlite3.Cursor:
            with self._lock:
                return self._conn.execute(sql, params)

        cursor = await asyncio.to_thread(_do)
        return _SyncCursorWrapper(cursor)

    async def executemany(self, sql: str, params_list: list[tuple[Any, ...]]) -> None:
        def _do() -> None:
            with self._lock:
                self._conn.executemany(sql, params_list)

        await asyncio.to_thread(_do)

    async def executescript(self, sql: str) -> None:
        # Caution: executescript is for static scripts only.
        def _do() -> None:
            with self._lock:
                self._conn.executescript(sql)

        await asyncio.to_thread(_do)

    async def commit(self) -> None:
        def _do() -> None:
            with self._lock:
                self._conn.commit()

        await asyncio.to_thread(_do)

    async def rollback(self) -> None:
        def _do() -> None:
            with self._lock:
                self._conn.rollback()

        await asyncio.to_thread(_do)

    async def close(self) -> None:
        def _do() -> None:
            with self._lock:
                self._conn.close()

        await asyncio.to_thread(_do)

    def cursor(self) -> _SyncCursorWrapper:
        return _SyncCursorWrapper(self._conn.cursor())


class _SyncCursorWrapper:
    """Wrap sync cursor to provide async interface."""

    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    async def fetchone(self) -> Any | None:
        return await asyncio.to_thread(self._cursor.fetchone)

    async def fetchall(self) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._cursor.fetchall)

    async def fetchmany(self, size: int) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._cursor.fetchmany, size)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid


class AsyncConnectionManager:
    """One connection per DB file. Platform-aware async wrapper."""

    def __init__(self, base_dir: str = ""):
        self.base_dir = Path(base_dir or _DEFAULT_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._conns: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def get(self, db_name: str = "memory.db") -> _SyncConnectionWrapper | Any:
        """Return (or create) a connection to `db_name`."""
        if db_name in self._conns:
            conn = self._conns[db_name]
            try:
                await conn.execute("SELECT 1")
                return conn
            except Exception:
                logger.warning(f"connection {db_name} stale, reopening")
                # pop, not del: a concurrent getter may have already replaced it
                self._conns.pop(db_name, None)

        db_path = str(self.base_dir / db_name)

        if _HAS_AIOSQLITE and not _USE_SYNC:
            conn = await self._get_aiosqlite_conn(db_path)
        else:
            conn = await self._get_sync_conn(db_path)

        self._conns[db_name] = conn
        logger.debug(f"opened connection {db_name} ({db_path}) [sync={_USE_SYNC}]")
        return conn

    async def _get_aiosqlite_conn(self, db_path: str) -> Any:
        """Create aiosqlite connection (Linux/macOS)."""
        import aiosqlite

        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        if _wal_enabled():
            await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA cache_size=-64000")  # 64MB page cache
        await conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    async def _get_sync_conn(self, db_path: str) -> _SyncConnectionWrapper:
        """Create sync sqlite3 connection wrapped for async (Windows)."""

        def _connect() -> sqlite3.Connection:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            if _wal_enabled():
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA cache_size=-64000")  # 64MB page cache
            conn.execute("PRAGMA temp_store=MEMORY")
            return conn

        raw_conn = await asyncio.to_thread(_connect)
        return _SyncConnectionWrapper(raw_conn)

    async def close_all(self) -> None:
        """Close all open connections (on shutdown)."""
        for name, conn in self._conns.items():
            with contextlib.suppress(Exception):
                await conn.close()
                logger.debug("closed connection %s", name)
        self._conns.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "connections": len(self._conns),
            "dbs": list(self._conns.keys()),
            "backend": "sync" if _USE_SYNC else "aiosqlite",
        }

    # ------------------------------------------------------------------
    # Helpers for migrations and init-db
    # ------------------------------------------------------------------

    async def execute_script(self, db_name: str, script: str) -> None:
        """Execute a SQL script (e.g. CREATE TABLE) and commit."""
        conn = await self.get(db_name)
        await conn.executescript(script)
        await conn.commit()

    async def table_exists(self, db_name: str, table: str) -> bool:
        """Check whether a table exists."""
        conn = await self.get(db_name)
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        row = await cur.fetchone()
        return row is not None

    async def vacuum(self, db_name: str) -> None:
        """VACUUM — reclaim space after bulk deletions."""
        conn = await self.get(db_name)
        await conn.execute("VACUUM")
        await conn.commit()


# Global instance — used by default
connection_manager = AsyncConnectionManager()
