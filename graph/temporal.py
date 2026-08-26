from __future__ import annotations

"""
Temporal Graph - time-based memory relations

Records significant memory events (thoughts, personality shifts, project
decisions) as a per-user, layer-scoped timeline. dream(intent="recent")
surfaces the newest entries as a timeline digest.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from shared.connection import connection_manager
from shared.constants import DB_NAME

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager

logger = logging.getLogger(__name__)


@dataclass
class TemporalEvent:
    event_id: int
    user_id: str
    event_type: str
    content: str
    timestamp: float
    importance: float
    metadata: dict[str, Any]


class TemporalGraph:
    def __init__(self, cm: AsyncConnectionManager | None = None) -> None:
        self._cm = cm or connection_manager
        self._ready = False

    async def ensure(self) -> None:
        """Lazy one-time schema setup (create/migrate), mirroring DreamBuffer."""
        if self._ready:
            return
        await self.init_db()
        self._ready = True

    async def init_db(self) -> None:
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS temporal_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL DEFAULT 'user',
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
        # PRAGMA-first migration for tables created before layers existed.
        # Every live install has 0 rows here, so backfill is trivially safe.
        conn = await self._cm.get(DB_NAME)
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(temporal_events)")).fetchall()]
        if cols and "layer" not in cols:
            await conn.execute("ALTER TABLE temporal_events ADD COLUMN layer TEXT NOT NULL DEFAULT 'user'")
            await conn.commit()
        # layer-dependent index last: it would fail on pre-layer tables
        await self._cm.execute_script(
            DB_NAME,
            "CREATE INDEX IF NOT EXISTS idx_temp_layer_user ON temporal_events(layer, user_id);",
        )

    async def add_event(
        self,
        user_id: str,
        event_type: str,
        content: str,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        layer: str = "user",
    ) -> int:
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO temporal_events (layer, user_id, event_type, content, timestamp, importance, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (layer, user_id, event_type, content[:500], time.time(), importance, json.dumps(metadata or {})),
        )
        await conn.commit()
        return int(cursor.lastrowid or 0)

    async def link_events(self, from_event: int, to_event: int, link_type: str = "follows", strength: float = 0.5) -> None:
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT OR REPLACE INTO temporal_links (from_event, to_event, link_type, strength) VALUES (?, ?, ?, ?)",
            (from_event, to_event, link_type, strength),
        )
        await conn.commit()

    @staticmethod
    def _row_to_event(r: Any) -> TemporalEvent:
        try:
            metadata = json.loads(r["metadata"]) if r["metadata"] else {}
        except (ValueError, TypeError):
            logger.warning("corrupt temporal metadata on event %s", r["event_id"])
            metadata = {}
        return TemporalEvent(
            event_id=r["event_id"],
            user_id=r["user_id"],
            event_type=r["event_type"],
            content=r["content"],
            timestamp=r["timestamp"],
            importance=r["importance"],
            metadata=metadata,
        )

    async def get_timeline(self, user_id: str, limit: int = 50, offset: int = 0, layer: str | None = None) -> list[TemporalEvent]:
        conn = await self._cm.get(DB_NAME)
        sql = "SELECT * FROM temporal_events WHERE user_id=?"
        params: list[Any] = [user_id]
        if layer:
            sql += " AND layer=?"
            params.append(layer)
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = await conn.execute(sql, tuple(params))
        rows = await cur.fetchall()
        return [self._row_to_event(r) for r in rows]

    async def get_recent(self, user_id: str, layer: str | None = None, limit: int = 5) -> list[TemporalEvent]:
        """Newest events for the dream('recent') timeline digest."""
        await self.ensure()
        return await self.get_timeline(user_id, limit=limit, layer=layer)

    async def get_events_near(self, user_id: str, timestamp: float, window_seconds: float = 3600, limit: int = 20) -> list[TemporalEvent]:
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM temporal_events WHERE user_id=? AND ABS(timestamp - ?) < ? ORDER BY timestamp LIMIT ?",
            (user_id, timestamp, window_seconds, limit),
        )
        rows = await cur.fetchall()
        return [self._row_to_event(r) for r in rows]

    async def get_causal_chain(self, event_id: int, direction: str = "forward", limit: int = 10, user_id: str | None = None) -> list[dict[str, Any]]:
        """Traverse links from/to one event.

        Pass user_id to keep traversal inside that user's timeline.
        """
        conn = await self._cm.get(DB_NAME)
        user_filter = " AND te.user_id=?" if user_id else ""
        if direction == "forward":
            sql = f"SELECT tl.to_event, te.event_type, te.content, te.timestamp FROM temporal_links tl JOIN temporal_events te ON tl.to_event = te.event_id WHERE tl.from_event = ?{user_filter} LIMIT ?"
        else:
            sql = f"SELECT tl.from_event, te.event_type, te.content, te.timestamp FROM temporal_links tl JOIN temporal_events te ON tl.from_event = te.event_id WHERE tl.to_event = ?{user_filter} LIMIT ?"
        params: tuple[Any, ...] = (event_id, limit) if not user_id else (event_id, user_id, limit)
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        return [{"event_id": r[0], "type": r[1], "content": r[2], "timestamp": r[3]} for r in rows]

    async def count_events(self, user_id: str | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if user_id:
            cur = await conn.execute("SELECT COUNT(*) FROM temporal_events WHERE user_id=?", (user_id,))
        else:
            cur = await conn.execute("SELECT COUNT(*) FROM temporal_events")
        row = await cur.fetchone()
        return row[0] if row else 0
