# autohooks/source.py
"""SQLite conversation-source driver (spec S3). Read-only; ariel-free."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from autohooks.config import AgentConfig, SourceConfig, sql_expr

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Message:
    sender: str
    text: str
    ts: float | None
    source_id: int


@dataclass(frozen=True)
class Batch:
    messages: list[Message] = field(default_factory=list)
    cursor: int = 0


class SqliteSource:
    """Read-only tail over one SQLite table. SQL is built once from config."""

    def __init__(self, db_path: Path, source: SourceConfig) -> None:
        self._db_path = db_path
        self._source = source
        s = source
        select = f"SELECT {s.cursor_column} AS _cursor, {sql_expr(s.role)} AS _role, {sql_expr(s.text)} AS _text"
        if s.ts is not None:
            select += f", {sql_expr(s.ts)} AS _ts"
        select += f" FROM {s.table} WHERE {s.cursor_column} > ?"
        if s.filter:
            select += f" AND ({s.filter})"
        select += f" ORDER BY {s.order_by} LIMIT ?"
        self._select = select
        self._max_sql = f"SELECT max({s.cursor_column}) FROM {s.table}"

    @classmethod
    def from_config(cls, cfg: AgentConfig) -> SqliteSource:
        if cfg.source.driver != "sqlite":
            raise ValueError(f"unsupported driver: {cfg.source.driver!r} (v1: sqlite only)")
        return cls(cfg.source.path, cfg.source)

    def connect(self) -> sqlite3.Connection:
        """Read-only connection — the daemon must never write agent DBs."""
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def max_id(self, conn: sqlite3.Connection) -> int:
        """First-run baseline: start at the newest existing row, no replay."""
        row = conn.execute(self._max_sql).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def fetch_after(self, conn: sqlite3.Connection, cursor: int, limit: int) -> Batch:
        rows = conn.execute(self._select, (cursor, limit)).fetchall()
        if not rows:
            return Batch(messages=[], cursor=cursor)
        keys = rows[0].keys()
        msgs = [
            Message(
                sender=str(r["_role"] or ""),
                text=str(r["_text"] or ""),
                ts=float(r["_ts"]) if "_ts" in keys and r["_ts"] is not None else None,
                source_id=int(r["_cursor"]),
            )
            for r in rows
        ]
        return Batch(messages=msgs, cursor=msgs[-1].source_id)
