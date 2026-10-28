"""F96: the daemon tail and the plugin push must not fight over one message.

Zone: hooks-delivery. One source message is captured once. Whichever path
wins — plugin push or daemon tail — the loser writes no rejection and loses
no provenance: the winning journal row carries the `source_msg_id` it came
from. The plugin side lives in another repository and cannot be changed, so
the daemon is the side that must yield, stamp the row it finds, and move its
cursor on.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from autohooks.config import AgentConfig, FieldMap, SourceConfig
from autohooks.daemon import load_cursor, run_daemon, save_cursor
from autohooks.source import SqliteSource

if TYPE_CHECKING:
    from pathlib import Path

TEXT = "решение по архитектуре: мост остаётся синхронным"
SOURCE_ID = 5


@pytest.fixture
async def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private memory base; never the live one (role F6)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(data_dir))

    from shared.connection import connection_manager
    from shared.migrations import MigrationManager

    original = connection_manager.base_dir
    connection_manager.base_dir = data_dir
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield data_dir
    connection_manager._conns.clear()
    connection_manager.base_dir = original


def _cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        data_dir=tmp_path / "data",
        user_id="u1",
        layer="user",
        source=SourceConfig(
            driver="sqlite",
            path=tmp_path / "conv.db",
            table="messages",
            cursor_column="id",
            order_by="id",
            role=FieldMap(column="role"),
            text=FieldMap(column="content"),
            ts=None,
            filter=None,
        ),
        poll_seconds=0.01,
        state_file=tmp_path / "cursor.json",
    )


def _source_db(tmp_path: Path) -> Path:
    path = tmp_path / "conv.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
    conn.execute("INSERT INTO messages (id, role, content) VALUES (?, 'user', ?)", (SOURCE_ID, TEXT))
    conn.commit()
    conn.close()
    return path


def _journal_row(data_dir: Path, rid: int) -> tuple[Any, ...]:
    conn = sqlite3.connect(data_dir / "memory.db")
    try:
        row = conn.execute("SELECT source_msg_id FROM l0_journal WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _rejections(data_dir: Path) -> int:
    conn = sqlite3.connect(data_dir / "memory.db")
    try:
        row = conn.execute("SELECT count(*) FROM memory_dispatch_rejections").fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


async def test_a_tick_over_an_already_captured_row_stamps_and_skips(tmp_path: Path, base: Path) -> None:
    """The plugin won the race; the daemon must neither refuse nor duplicate."""
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", TEXT)
    assert rid is not None
    assert _journal_row(base, rid)[0] is None

    dispatched: list[dict[str, Any]] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        dispatched.append(payload)
        return {"results": [], "handler_count": 0}

    _source_db(tmp_path)
    save_cursor(tmp_path / "cursor.json", 0)
    await run_daemon(_cfg(tmp_path), SqliteSource.from_config(_cfg(tmp_path)), mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert dispatched == [], "an already-captured message must not be dispatched a second time"
    assert load_cursor(tmp_path / "cursor.json") == SOURCE_ID, "the cursor must still move past the skipped row"
    assert _journal_row(base, rid)[0] == SOURCE_ID, "the winning row must carry the source id it came from"
    assert _rejections(base) == 0, "the losing path writes no rejection"


async def test_a_tick_over_a_new_row_delivers_normally(tmp_path: Path, base: Path) -> None:
    """No prior row: the daemon is the winning path and dispatches as before."""
    dispatched: list[dict[str, Any]] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        dispatched.append(payload)
        return {"results": [], "handler_count": 0}

    _source_db(tmp_path)
    save_cursor(tmp_path / "cursor.json", 0)
    await run_daemon(_cfg(tmp_path), SqliteSource.from_config(_cfg(tmp_path)), mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert len(dispatched) == 1
    assert dispatched[0]["text"] == TEXT
    assert dispatched[0]["source_msg_id"] == SOURCE_ID
    assert load_cursor(tmp_path / "cursor.json") == SOURCE_ID
    assert _rejections(base) == 0


async def test_stamping_does_not_overwrite_an_existing_source_id(tmp_path: Path, base: Path) -> None:
    """A row that already names its source keeps that name."""
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", TEXT, source_msg_id=99)
    assert rid is not None
    assert _journal_row(base, rid)[0] == 99

    dispatched: list[dict[str, Any]] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        dispatched.append(payload)
        return {"results": [], "handler_count": 0}

    _source_db(tmp_path)
    save_cursor(tmp_path / "cursor.json", 0)
    await run_daemon(_cfg(tmp_path), SqliteSource.from_config(_cfg(tmp_path)), mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert dispatched == []
    assert load_cursor(tmp_path / "cursor.json") == SOURCE_ID
    assert _journal_row(base, rid)[0] == 99, "the existing provenance must win"


async def test_a_second_tick_over_the_same_row_is_a_no_op(tmp_path: Path, base: Path) -> None:
    """Re-reading the same source row (a rewound cursor) changes nothing."""
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", TEXT)

    dispatched: list[dict[str, Any]] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        dispatched.append(payload)
        return {"results": [], "handler_count": 0}

    _source_db(tmp_path)
    save_cursor(tmp_path / "cursor.json", 0)
    await run_daemon(_cfg(tmp_path), SqliteSource.from_config(_cfg(tmp_path)), mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)
    assert _journal_row(base, rid)[0] == SOURCE_ID

    save_cursor(tmp_path / "cursor.json", 0)
    await run_daemon(_cfg(tmp_path), SqliteSource.from_config(_cfg(tmp_path)), mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert dispatched == [], "the second read is already captured"
    assert load_cursor(tmp_path / "cursor.json") == SOURCE_ID
    assert _journal_row(base, rid)[0] == SOURCE_ID
    assert _rejections(base) == 0
