"""tool_stats must read through the write dispatch, not through the shared handle.

`AsyncConnectionManager.get(DB_NAME)` hands out ONE connection per database to
every component in the process. A reader that keeps a cursor on that handle pins
a read snapshot there, and a foreign commit then turns the next write on that
handle into `SQLITE_BUSY_SNAPSHOT` (517). The dispatch (`shared/write_dispatch.py`)
is the process's only owner of transactions: it opens, reads and closes every
cursor inside one method body, so no snapshot is ever held between calls.

These tests pin the discipline, not the numbers: the statistics themselves are
covered by `tests/test_lifecycle/test_tool_triplets.py`.
"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager
from shared.write_dispatch import WriteDispatch, get_write_dispatch


@pytest.fixture
async def db(tmp_path):
    """A migrated base in a tmp dir — the real `~/.mcp-ariel-memory` is never touched."""
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


class _QuerySpy:
    """A dispatch that records the SQL it was asked to run."""

    def __init__(self, real: WriteDispatch) -> None:
        self._real = real
        self.queries: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def query(self, db: str, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return await self._real.query(db, sql, params)


async def _l0_tool(text: str, raw_type: str, ts: float | None = None) -> None:
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type) VALUES (?, 'new_message', NULL, 'user', 'gu', ?, ?)",
        (ts if ts is not None else time.time(), text, raw_type),
    )
    await conn.commit()


def _use(tid: str, name: str, query: str) -> str:
    return json.dumps([{"type": "tool_use", "id": tid, "name": name, "input": {"query": query}}])


def _result(tid: str, content: str) -> str:
    return json.dumps([{"type": "tool_result", "tool_use_id": tid, "content": content}])


async def test_tool_behavior_stats_reads_through_the_write_dispatch(db, monkeypatch):
    """The statistics are read on the dispatch, not on the shared handle."""
    await _l0_tool(_use("t1", "search", "как настроить postgres wal"), "tool_use")
    await _l0_tool(_result("t1", "wal_level=replica"), "tool_result")

    from lifecycle.tool_stats import tool_behavior_stats

    spy = _QuerySpy(get_write_dispatch(connection_manager))
    monkeypatch.setattr("lifecycle.tool_stats.get_write_dispatch", lambda cm=None: spy, raising=False)

    stats = await tool_behavior_stats()

    assert spy.queries, "the statistics were not read through the dispatch"
    assert any("l0_journal" in sql for sql in spy.queries), spy.queries
    # The happy path survives the move: the numbers are still the numbers.
    assert stats["search"]["calls"] == 1
    assert stats["search"]["avg_result_len"] > 0


async def test_tool_behavior_stats_does_not_take_the_shared_connection(db, monkeypatch):
    """The shared handle is not the reader's business: no `get(DB_NAME)` on it.

    The shared handle is one connection for the whole process. A component that
    reads on it can pin a snapshot there, and the next writer on that handle
    fails with 517. The dispatch keeps its own connection for exactly this.
    """
    await _l0_tool(_use("t1", "search", "как настроить postgres wal"), "tool_use")
    await _l0_tool(_result("t1", "wal_level=replica"), "tool_result")

    from lifecycle.tool_stats import tool_behavior_stats

    borrowed: list[str] = []
    original_get = connection_manager.get

    async def _tracking_get(db_name: str = "memory.db") -> Any:
        borrowed.append(db_name)
        return await original_get(db_name)

    monkeypatch.setattr(connection_manager, "get", _tracking_get)

    await tool_behavior_stats()

    assert DB_NAME not in borrowed, f"the shared handle was used for a read: {borrowed}"


async def test_scan_tool_pairs_takes_the_dispatch_not_a_connection(db):
    """The scanner's first argument is the dispatch, not a raw connection."""
    from lifecycle.tool_stats import scan_tool_pairs

    parameters = list(inspect.signature(scan_tool_pairs).parameters)
    assert "conn" not in parameters, f"scan_tool_pairs still takes a raw connection: {parameters}"

    await _l0_tool(_use("t1", "search", "как настроить postgres wal"), "tool_use")
    await _l0_tool(_result("t1", "wal_level=replica"), "tool_result")

    pairs, calls = await scan_tool_pairs(get_write_dispatch(connection_manager), 0.0, layer="user")

    assert calls == {"search": 1}, calls
    assert len(pairs) == 1, pairs
