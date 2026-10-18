from __future__ import annotations

"""
Rate Limiter — async SQLite-based per-user rate limiting + WebSocket connection limiting
"""

import threading
import time
from typing import Any, cast

from config import config
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch, get_write_dispatch


class RateLimiter:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager
        self._max_per_user = config.get("security", "rate_limit_per_user") or 100
        self._window_seconds = 60

    @property
    def _dispatch(self) -> WriteDispatch:
        """Resolved on every access so a repointed ``_cm`` is honoured."""
        return get_write_dispatch(self._cm)

    async def _init_db(self) -> None:
        await self._dispatch.execute_script(
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
        # This runs at the top of every tool call (`mcp_server/tools/base.py:149`), so it is
        # the widest-reaching writer in the process. With a foreign transaction open on the
        # shared connection it records nothing rather than waiting on the write lock or
        # flushing work it does not own: skipping one rate-limit tick is cheaper than a
        # stalled tool call.
        shared = await self._cm.get(DB_NAME)
        if shared.in_transaction:
            return {"allowed": True, "remaining": self._max_per_user}

        async def _txn(conn: Any) -> dict[str, Any]:
            await conn.execute("DELETE FROM rate_limits WHERE timestamp < ?", (cutoff,))
            await conn.execute("INSERT INTO rate_limits (user_id, timestamp) VALUES (?, ?)", (user_id, now))
            cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM rate_limits WHERE user_id=? AND timestamp >= ?",
                (user_id, cutoff),
            )
            row = await cursor.fetchone()
            count = row["cnt"] if row else 0
            if count > self._max_per_user:
                cursor = await conn.execute(
                    "SELECT MIN(timestamp) as ts FROM rate_limits WHERE user_id=? AND timestamp >= ?",
                    (user_id, cutoff),
                )
                oldest = await cursor.fetchone()
                reset_in = int(self._window_seconds - (now - (oldest["ts"] if oldest else now)))
                return {"allowed": False, "remaining": 0, "reset_in": max(reset_in, 1)}
            return {"allowed": True, "remaining": self._max_per_user - count}

        return cast("dict[str, Any]", await self._dispatch.atomic(DB_NAME, _txn))

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        cutoff = time.time() - self._window_seconds
        value = await self._dispatch.scalar(
            DB_NAME,
            "SELECT COUNT(*) as cnt FROM rate_limits WHERE user_id=? AND timestamp >= ?",
            (user_id, cutoff),
        )
        return {"requests_last_minute": int(value) if value is not None else 0, "limit": self._max_per_user}

    async def cleanup_old(self) -> int:
        cutoff = time.time() - (self._window_seconds * 10)
        # Same shape as `check()` above: never ride or flush another component's transaction.
        shared = await self._cm.get(DB_NAME)
        if shared.in_transaction:
            return 0
        return int(await self._dispatch.write(DB_NAME, "DELETE FROM rate_limits WHERE timestamp < ?", (cutoff,)))


class ConnectionLimiter:
    """Async WebSocket/SSE connection limiter (in-memory)."""

    def __init__(self, max_connections_per_user: int | None = None, max_total: int | None = None):
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

    def release(self, user_id: str, connection_id: str) -> None:
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
