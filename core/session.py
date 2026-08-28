from __future__ import annotations

"""
L2 SessionStore — async session history with indexes.

v1.8.1+ adds 2 columns (quality_score, quality_parts) computed at
close_session time. See core/session_quality.py for the scorer.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from .session_quality import (
    compute_session_quality,
    parts_from_json,
    parts_to_json,
)

logger = logging.getLogger(__name__)


@dataclass
class SessionRecord:
    session_id: str
    user_id: str
    summary: str
    state_deltas: dict[str, Any] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    message_count: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0
    quality_score: float | None = None
    quality_parts: dict[str, float] | None = None


class SessionStore:
    def __init__(self, cm: AsyncConnectionManager | None = None) -> None:
        self._cm = cm or connection_manager

    async def _init_db(self) -> None:
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
            CREATE INDEX IF NOT EXISTS idx_sessions_user_time ON sessions(user_id, started_at DESC);
            """,
        )
        # PRAGMA-first migration for the 2 quality-score columns.
        # Idempotent: skipped when columns already exist (live installs).
        # NOTE: alembic creates sessions; this is belt-and-suspenders for
        # direct _init_db callers (tests). Runtime path goes through
        # _ensure_quality_columns().
        conn = await self._cm.get(DB_NAME)
        await self._ensure_quality_columns(conn)

    async def create_session(self, user_id: str) -> str:
        session_id = f"sess_{user_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, started_at) VALUES (?, ?, ?)",
            (session_id, user_id, time.time()),
        )
        await conn.commit()
        return session_id

    async def _ensure_quality_columns(self, conn: Any) -> None:
        """Idempotent PRAGMA migration: add quality columns if missing."""
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(sessions)")).fetchall()]
        if cols and "quality_score" not in cols:
            await conn.execute("ALTER TABLE sessions ADD COLUMN quality_score REAL")
        if cols and "quality_parts" not in cols:
            await conn.execute("ALTER TABLE sessions ADD COLUMN quality_parts TEXT")
        await conn.commit()

    async def close_session(
        self,
        session_id: str,
        summary: str = "",
        state_deltas: dict[str, Any] | None = None,
        topics: list[str] | None = None,
        message_count: int = 0,
    ) -> None:
        """Close session and compute quality score. Scoring is non-fatal."""
        topics = topics or []
        state_deltas = state_deltas or {}
        conn = await self._cm.get(DB_NAME)
        await self._ensure_quality_columns(conn)

        row = await (
            await conn.execute(
                "SELECT user_id, started_at, message_count FROM sessions WHERE session_id=?",
                (session_id,),
            )
        ).fetchone()
        if row is None:
            logger.warning("SessionStore.close_session: no row for %s", session_id)
            return

        score: float | None = None
        parts_json: str | None = None
        try:
            ended_at = time.time()
            score, parts = await compute_session_quality(
                self._cm,
                row["user_id"],
                started_at=row["started_at"],
                ended_at=ended_at,
                message_count=message_count or row["message_count"] or 0,
                topics=topics,
                state_deltas=state_deltas,
            )
            parts_json = parts_to_json(parts)
        except Exception as exc:
            logger.warning("SessionStore.close_session: scoring failed for %s: %s", session_id, exc)

        await conn.execute(
            "UPDATE sessions SET summary=?, state_deltas=?, topics=?, ended_at=?, quality_score=?, quality_parts=? WHERE session_id=?",
            (
                summary,
                json.dumps(state_deltas),
                json.dumps(topics),
                time.time(),
                score,
                parts_json,
                session_id,
            ),
        )
        await conn.commit()

    async def get_recent_sessions(self, user_id: str, limit: int = 10) -> list[SessionRecord]:
        conn = await self._cm.get(DB_NAME)
        await self._ensure_quality_columns(conn)
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
        return int(row[0]) if row else 0

    async def avg_quality(self, user_id: str | None = None) -> float | None:
        """Average quality_score over scored (non-NULL) sessions. None if none scored."""
        conn = await self._cm.get(DB_NAME)
        await self._ensure_quality_columns(conn)
        if user_id:
            cursor = await conn.execute(
                "SELECT AVG(quality_score) FROM sessions WHERE user_id=? AND quality_score IS NOT NULL",
                (user_id,),
            )
        else:
            cursor = await conn.execute("SELECT AVG(quality_score) FROM sessions WHERE quality_score IS NOT NULL")
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def _row_to_record(self, row: dict[str, Any] | Any) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            summary=row["summary"] or "",
            state_deltas=json.loads(row["state_deltas"]) if row["state_deltas"] else {},
            topics=json.loads(row["topics"]) if row["topics"] else [],
            message_count=row["message_count"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            quality_score=row["quality_score"],
            quality_parts=parts_from_json(row["quality_parts"]),
        )
