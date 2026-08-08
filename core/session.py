from __future__ import annotations
"""
L2 SessionStore — async session history with indexes
"""

import json
import time
import uuid
from dataclasses import dataclass, field

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME


@dataclass
class SessionRecord:
    session_id: str
    user_id: str
    summary: str
    state_deltas: dict = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    message_count: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0


class SessionStore:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def _init_db(self):
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                summary TEXT,
                state_deltas TEXT,
                topics TEXT,
                message_count INTEGER DEFAULT 0,
                started_at REAL NOT NULL,
                ended_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_time ON sessions(started_at);
        """,
        )

    async def create_session(self, user_id: str) -> str:
        session_id = f"sess_{user_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, started_at) VALUES (?, ?, ?)",
            (session_id, user_id, time.time()),
        )
        await conn.commit()
        return session_id

    async def close_session(self, session_id: str, summary: str = "", state_deltas: dict | None = None, topics: list[str] | None = None):
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "UPDATE sessions SET summary=?, state_deltas=?, topics=?, ended_at=? WHERE session_id=?",
            (summary, json.dumps(state_deltas or {}), json.dumps(topics or []), time.time(), session_id),
        )
        await conn.commit()

    async def get_recent_sessions(self, user_id: str, limit: int = 10) -> list["SessionRecord"]:
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT * FROM sessions WHERE user_id=? ORDER BY started_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(r) for r in rows]

    async def get_session_summary(self, user_id: str) -> str:
        sessions = await self.get_recent_sessions(user_id, 3)
        if not sessions:
            return "No sessions yet."
        return "\n".join([f"- {s.summary[:80]}" for s in sessions if s.summary])

    async def count_sessions(self, user_id: str | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if user_id:
            cursor = await conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,))
        else:
            cursor = await conn.execute("SELECT COUNT(*) FROM sessions")
        row = await cursor.fetchone()
        return row[0] if row else 0

    def _row_to_record(self, row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            summary=row["summary"] or "",
            state_deltas=json.loads(row["state_deltas"]) if row["state_deltas"] else {},
            topics=json.loads(row["topics"]) if row["topics"] else [],
            message_count=row["message_count"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )
