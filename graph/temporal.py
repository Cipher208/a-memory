"""
Temporal Graph - time-based memory relations
"""

import time
from dataclasses import dataclass
from typing import Any, Optional

from shared.connection import connection_manager
from shared.constants import DB_NAME


@dataclass
class TemporalEvent:
    event_id: int
    user_id: str
    event_type: str
    content: str
    timestamp: float
    importance: float
    metadata: dict


class TemporalGraph:
    def __init__(self, cm=None):
        self._cm = cm or connection_manager

    async def init_db(self):
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS temporal_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                importance REAL DEFAULT 0.5,
                metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS temporal_links (
                from_event INTEGER NOT NULL,
                to_event INTEGER NOT NULL,
                link_type TEXT NOT NULL DEFAULT 'follows',
                strength REAL DEFAULT 0.5,
                PRIMARY KEY (from_event, to_event, link_type)
            );
            CREATE INDEX IF NOT EXISTS idx_temp_user ON temporal_events(user_id);
            CREATE INDEX IF NOT EXISTS idx_temp_time ON temporal_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_temp_type ON temporal_events(event_type);
        """,
        )

    async def add_event(self, user_id: str, event_type: str, content: str, importance: float = 0.5, metadata: Optional[dict] = None) -> int:
        import json

        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO temporal_events (user_id, event_type, content, timestamp, importance, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, event_type, content, time.time(), importance, json.dumps(metadata or {})),
        )
        await conn.commit()
        return cursor.lastrowid

    async def link_events(self, from_event: int, to_event: int, link_type: str = "follows", strength: float = 0.5):
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT OR REPLACE INTO temporal_links (from_event, to_event, link_type, strength) VALUES (?, ?, ?, ?)",
            (from_event, to_event, link_type, strength),
        )
        await conn.commit()

    async def get_timeline(self, user_id: str, limit: int = 50, offset: int = 0) -> list[TemporalEvent]:
        import json

        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM temporal_events WHERE user_id=? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [
            TemporalEvent(
                event_id=r["event_id"],
                user_id=r["user_id"],
                event_type=r["event_type"],
                content=r["content"],
                timestamp=r["timestamp"],
                importance=r["importance"],
                metadata=json.loads(r["metadata"]) if r["metadata"] else {},
            )
            for r in rows
        ]

    async def get_events_near(self, user_id: str, timestamp: float, window_seconds: float = 3600, limit: int = 20) -> list[TemporalEvent]:
        import json

        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM temporal_events WHERE user_id=? AND ABS(timestamp - ?) < ? ORDER BY timestamp LIMIT ?",
            (user_id, timestamp, window_seconds, limit),
        )
        rows = await cur.fetchall()
        return [
            TemporalEvent(
                event_id=r["event_id"],
                user_id=r["user_id"],
                event_type=r["event_type"],
                content=r["content"],
                timestamp=r["timestamp"],
                importance=r["importance"],
                metadata=json.loads(r["metadata"]) if r["metadata"] else {},
            )
            for r in rows
        ]

    async def get_causal_chain(self, event_id: int, direction: str = "forward", limit: int = 10) -> list[dict[str, Any]]:
        conn = await self._cm.get(DB_NAME)
        if direction == "forward":
            sql = "SELECT tl.to_event, te.event_type, te.content, te.timestamp FROM temporal_links tl JOIN temporal_events te ON tl.to_event = te.event_id WHERE tl.from_event = ? LIMIT ?"
        else:
            sql = "SELECT tl.from_event, te.event_type, te.content, te.timestamp FROM temporal_links tl JOIN temporal_events te ON tl.from_event = te.event_id WHERE tl.to_event = ? LIMIT ?"
        cur = await conn.execute(sql, (event_id, limit))
        rows = await cur.fetchall()
        return [{"event_id": r[0], "type": r[1], "content": r[2], "timestamp": r[3]} for r in rows]

    async def count_events(self, user_id: Optional[str] = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if user_id:
            cur = await conn.execute("SELECT COUNT(*) FROM temporal_events WHERE user_id=?", (user_id,))
        else:
            cur = await conn.execute("SELECT COUNT(*) FROM temporal_events")
        row = await cur.fetchone()
        return row[0] if row else 0
