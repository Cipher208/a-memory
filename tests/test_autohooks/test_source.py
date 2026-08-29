# tests/test_autohooks/test_source.py
"""SQLite driver: cursor query, filter, json_path mapping (spec S3)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from autohooks.config import load_config
from autohooks.source import SqliteSource

if TYPE_CHECKING:
    from pathlib import Path

PLAIN = """
data_dir: {tmp}/data
user_id: u1
source:
  driver: sqlite
  path: {tmp}/conv.db
  table: messages
  cursor_column: id
  order_by: id
  role: {{column: role}}
  text: {{column: content}}
  ts: {{column: timestamp}}
  filter: "role IN ('user', 'assistant')"
"""

EVENT = """
data_dir: {tmp}/data
user_id: u1
source:
  driver: sqlite
  path: {tmp}/conv.db
  table: event
  cursor_column: rowid
  order_by: rowid
  role: {{json_path: [data, '$.role']}}
  text: {{json_path: [data, '$.text']}}
  filter: "type = 'message'"
"""


def _make_plain_db(tmp_path: Path) -> Path:
    db = tmp_path / "conv.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp REAL)")
    rows = [
        (1, "user", "привет", 100.0),
        (2, "tool", "tool junk", 101.0),
        (3, "assistant", "отвечаю подробно", 102.0),
    ]
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def _make_event_db(tmp_path: Path) -> None:
    db = tmp_path / "conv.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE event (id TEXT PRIMARY KEY, type TEXT, data TEXT)")
    conn.executemany(
        "INSERT INTO event VALUES (?, ?, ?)",
        [
            ("e1", "message", '{"role": "user", "text": "событийная строка"}'),
            ("e2", "session_start", '{"x": 1}'),
            ("e3", "message", '{"role": "assistant", "text": "ответ событием"}'),
        ],
    )
    conn.commit()
    conn.close()


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _cfg(tmp_path: Path, template: str):
    if not (tmp_path / "conv.db").exists():
        _make_plain_db(tmp_path)
    return load_config(_write(tmp_path, template.format(tmp=tmp_path)))


def test_fetch_after_respects_cursor_and_filter(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, PLAIN)
    src = SqliteSource.from_config(cfg)
    with src.connect() as conn:
        batch = src.fetch_after(conn, cursor=1, limit=100)
    assert [m.source_id for m in batch.messages] == [3]
    assert batch.messages[0].sender == "assistant"
    assert batch.cursor == 3


def test_empty_batch_keeps_cursor(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, PLAIN)
    src = SqliteSource.from_config(cfg)
    with src.connect() as conn:
        batch = src.fetch_after(conn, cursor=3, limit=100)
    assert batch.messages == []
    assert batch.cursor == 3


def test_max_id_baseline(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, PLAIN)
    src = SqliteSource.from_config(cfg)
    with src.connect() as conn:
        assert src.max_id(conn) == 3


def test_json_path_extraction_event_store(tmp_path: Path) -> None:
    _make_event_db(tmp_path)
    cfg = load_config(_write(tmp_path, EVENT.format(tmp=tmp_path)))
    src = SqliteSource.from_config(cfg)
    with src.connect() as conn:
        batch = src.fetch_after(conn, cursor=0, limit=100)
    assert [m.text for m in batch.messages] == ["событийная строка", "ответ событием"]
    assert [m.sender for m in batch.messages] == ["user", "assistant"]


def test_limit_respected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, PLAIN)
    src = SqliteSource.from_config(cfg)
    with src.connect() as conn:
        batch = src.fetch_after(conn, cursor=0, limit=1)
    assert [m.source_id for m in batch.messages] == [1]


def test_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, PLAIN)
    src = SqliteSource.from_config(cfg)
    with pytest.raises(sqlite3.OperationalError), src.connect() as conn:
        conn.execute("DELETE FROM messages")
        conn.commit()


def test_unsupported_driver_rejected(tmp_path: Path) -> None:
    body = PLAIN.format(tmp=tmp_path).replace("driver: sqlite", "driver: jsonl")
    with pytest.raises(ValueError, match="unsupported driver"):
        SqliteSource.from_config(load_config(_write(tmp_path, body)))
