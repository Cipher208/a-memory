"""A miner must read and write on the SAME connection.

`_maintenance` runs a miner on the shared handle when the caller already holds
a transaction there (the borrowed branch) and on the dispatch's own connection
otherwise. The borrowed branch used to read the L0 journal through the
dispatch — a DIFFERENT connection — so the read and the write saw different
snapshots: a journal row the miner itself had inserted but not committed was
invisible to it.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """A migrated base in a tmp dir — the real `~/.mcp-ariel-memory` is never touched.

    The busy timeout is shortened on purpose: the dispatch's connection cannot
    take the write lock while the shared handle holds it, and the test below
    holds it deliberately.
    """
    monkeypatch.setattr("shared.connection.BUSY_TIMEOUT_MS", 250)
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


def _use(tid: str, name: str, query: str) -> str:
    return json.dumps([{"type": "tool_use", "id": tid, "name": name, "input": {"query": query}}])


def _result(tid: str, content: str) -> str:
    return json.dumps([{"type": "tool_result", "tool_use_id": tid, "content": content}])


async def _journal(conn: Any, text: str, raw_type: str) -> None:
    await conn.execute(
        "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type) VALUES (?, 'new_message', NULL, 'user', 'gu', ?, ?)",
        (time.time(), text, raw_type),
    )


async def test_borrowed_miner_sees_the_uncommitted_journal_row(db):
    """The borrowed branch must read the journal on the connection it writes on.

    The miner writes on the shared handle (the branch exists for a caller that
    already holds a transaction there); reading the journal through the
    dispatch meant reading a different connection, on a different snapshot:
    the row the caller had inserted was invisible to the miner.
    """
    conn = await connection_manager.get(DB_NAME)
    await _journal(conn, _use("t1", "search", "a query"), "tool_use")
    await _journal(conn, _result("t1", "a result"), "tool_result")
    assert conn.in_transaction, "the setup must leave a real pending transaction"

    from lifecycle.graph_miners import miner_tool_triplets

    result = await miner_tool_triplets(db, "user")

    assert result["edges"] == 2, "the miner read the journal on another connection and saw nothing"
