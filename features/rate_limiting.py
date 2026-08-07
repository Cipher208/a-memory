"""
Rate Limiter — async SQLite-based per-user rate limiting + WebSocket connection limiting
"""

import threading
import time
from typing import Any, Optional

from config import config
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


class RateLimiter:
    def __init__(self, cm: Optional["AsyncConnectionManager"] = None):
        self._cm = cm or connection_manager
        self._max_per_user = config.get("security", "rate_limit_per_user") or 100
        self._window_seconds = 60

    async def _init_db(self):
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rl_user ON rate_limits(user_id);
        """,
        )

    async def check(self, user_id: str) -> dict[str, Any]:
        now = time.time()
        cutoff = now - self._window_seconds
        conn = await self._cm.get(DB_NAME)
        await conn.execute("DELETE FROM rate_limits WHERE timestamp < ?", (cutoff,))
        await conn.execute("INSERT INTO rate_limits (user_id, timestamp) VALUES (?, ?)", (user_id, now))
        cursor = await conn.execute(
            "SELECT COUNT(*) as cnt FROM rate_limits WHERE user_id=? AND timestamp >= ?",
            (user_id, cutoff),
        )
        row = await cursor.fetchone()
        count = row["cnt"] if row else 0
        await conn.commit()

        if count > self._max_per_user:
            cursor = await conn.execute(
                "SELECT MIN(timestamp) as ts FROM rate_limits WHERE user_id=? AND timestamp >= ?",
                (user_id, cutoff),
            )
            oldest = await cursor.fetchone()
            reset_in = int(self._window_seconds - (now - (oldest["ts"] if oldest else now)))
            return {"allowed": False, "remaining": 0, "reset_in": max(reset_in, 1)}

        return {"allowed": True, "remaining": self._max_per_user - count}

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        cutoff = time.time() - self._window_seconds
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT COUNT(*) as cnt FROM rate_limits WHERE user_id=? AND timestamp >= ?",
            (user_id, cutoff),
        )
        row = await cursor.fetchone()
        return {"requests_last_minute": row["cnt"] if row else 0, "limit": self._max_per_user}

    async def cleanup_old(self) -> int:
        cutoff = time.time() - (self._window_seconds * 10)
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute("DELETE FROM rate_limits WHERE timestamp < ?", (cutoff,))
        await conn.commit()
        return cursor.rowcount


class ConnectionLimiter:
    """Async WebSocket/SSE connection limiter (in-memory)."""

    def __init__(self, max_connections_per_user: Optional[int] = None, max_total: Optional[int] = None):
        self._max_per_user = max_connections_per_user or config.get("security", "max_ws_per_user") or 5
        self._max_total = max_total or config.get("security", "max_ws_total") or 100
        self._connections: dict[str, set[str]] = {}
        self._total = 0
        self._lock = threading.Lock()

    def acquire(self, user_id: str, connection_id: str) -> dict[str, Any]:
        with self._lock:
            user_conns = self._connections.setdefault(user_id, set())
            if len(user_conns) >= self._max_per_user:
                return {"allowed": False, "reason": "user_limit", "current": len(user_conns), "max": self._max_per_user}
            if self._total >= self._max_total:
                return {"allowed": False, "reason": "total_limit", "current": self._total, "max": self._max_total}
            user_conns.add(connection_id)
            self._total += 1
            return {"allowed": True, "user_connections": len(user_conns), "total_connections": self._total}

    def release(self, user_id: str, connection_id: str):
        with self._lock:
            user_conns = self._connections.get(user_id, set())
            user_conns.discard(connection_id)
            self._total = max(0, self._total - 1)
            if not user_conns:
                self._connections.pop(user_id, None)

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_connections": self._total,
                "max_total": self._max_total,
                "max_per_user": self._max_per_user,
                "users": {uid: len(c) for uid, c in self._connections.items()},
            }
