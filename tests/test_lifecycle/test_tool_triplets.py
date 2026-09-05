"""Task C3 (S6b): минер #10 tool-триплеты + behavior-статистика.

L0: tool_use(id, name, input) + tool_result(tool_use_id, content, is_error),
связка по tool_use_id → узлы query/action/outcome (is_error → error_outcome)
и рёбра query_tool/tool_outcome (weight=0.5, tags heuristic:triplets).
tool_behavior_stats: per-tool calls, errors, error_rate, avg_result_len.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[Any]:
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _l0_tool(text: str, raw_type: str, ts: float | None = None) -> None:
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type) VALUES (?, 'new_message', NULL, 'user', 'gu', ?, ?)",
        (ts if ts is not None else time.time(), text, raw_type),
    )
    await conn.commit()


def _use(tid: str, name: str, query: str) -> str:
    return json.dumps([{"type": "tool_use", "id": tid, "name": name, "input": {"query": query}}])


def _result(tid: str, content: str, *, is_error: bool = False) -> str:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    return json.dumps([block])


async def test_miner_tool_triplets_pairs_nodes_and_error_node(db):
    await _l0_tool(_use("t1", "search", "как настроить postgres wal"), "tool_use")
    await _l0_tool(_result("t1", "wal_level=replica в конфиге"), "tool_result")
    await _l0_tool(_use("t2", "search", "удалить таблицу users"), "tool_use")
    await _l0_tool(_result("t2", "error: permission denied", is_error=True), "tool_result")

    from lifecycle.graph_miners import miner_tool_triplets

    result = await miner_tool_triplets(db, "user")
    assert result["edges"] == 4  # 2× query_tool + 2× tool_outcome

    conn = await connection_manager.get(DB_NAME)
    nodes = await (await conn.execute("SELECT content, node_type FROM epi_nodes")).fetchall()
    by_content = {r["content"]: r["node_type"] for r in nodes}
    assert by_content["как настроить postgres wal"] == "query"
    assert by_content["удалить таблицу users"] == "query"
    assert by_content["tool:search"] == "action"  # общий action-узел обоих вызовов
    assert by_content["wal_level=replica в конфиге"] == "outcome"
    assert by_content["error: permission denied"] == "error_outcome"

    edges = await (await conn.execute("SELECT relation, weight, tags FROM epi_edges ORDER BY relation, rowid")).fetchall()
    assert {r["relation"] for r in edges} == {"query_tool", "tool_outcome"}
    for r in edges:
        assert r["weight"] == 0.5
        assert "heuristic:triplets" in r["tags"]


async def test_miner_tool_triplets_rerun_is_noop(db):
    await _l0_tool(_use("t1", "search", "как настроить postgres wal"), "tool_use")
    await _l0_tool(_result("t1", "wal_level=replica в конфиге"), "tool_result")

    from lifecycle.graph_miners import miner_tool_triplets

    assert (await miner_tool_triplets(db, "user"))["edges"] == 2
    assert (await miner_tool_triplets(db, "user"))["edges"] == 0
    conn = await connection_manager.get(DB_NAME)
    nodes = await (await conn.execute("SELECT COUNT(*) FROM epi_nodes")).fetchone()
    edges = await (await conn.execute("SELECT COUNT(*) FROM epi_edges")).fetchone()
    assert nodes[0] == 3  # query + action + outcome
    assert edges[0] == 2


async def test_miner_tool_triplets_skips_unpaired_and_non_json(db):
    await _l0_tool(_use("t9", "search", "висячий вызов без результата"), "tool_use")
    await _l0_tool(_result("t8", "результат без вызова"), "tool_result")
    await _l0_tool("не JSON вовсе с tool_use_id=abc", "tool_result")  # classify_raw-мусор

    from lifecycle.graph_miners import miner_tool_triplets

    assert (await miner_tool_triplets(db, "user"))["edges"] == 0
    conn = await connection_manager.get(DB_NAME)
    nodes = await (await conn.execute("SELECT COUNT(*) FROM epi_nodes")).fetchone()
    assert nodes[0] == 0


async def test_miner_tool_triplets_handles_single_dict_blocks(db):
    await _l0_tool('{"type": "tool_use", "id": "d1", "name": "grep", "input": {"pattern": "foo"}}', "tool_use")
    await _l0_tool(
        '{"type": "tool_result", "tool_use_id": "d1", "content": [{"type": "text", "text": "найдено 3 совпадения"}]}',
        "tool_result",
    )

    from lifecycle.graph_miners import miner_tool_triplets

    assert (await miner_tool_triplets(db, "user"))["edges"] == 2
    conn = await connection_manager.get(DB_NAME)
    row = await (await conn.execute("SELECT content FROM epi_nodes WHERE node_type='outcome'")).fetchone()
    assert row["content"] == "найдено 3 совпадения"


async def test_tool_behavior_stats_calls_errors_rate(db):
    await _l0_tool(_use("t1", "search", "как настроить postgres wal"), "tool_use")
    await _l0_tool(_result("t1", "wal_level=replica"), "tool_result")
    await _l0_tool(_use("t2", "search", "удалить таблицу users"), "tool_use")
    await _l0_tool(_result("t2", "error: permission denied", is_error=True), "tool_result")

    from lifecycle.tool_stats import tool_behavior_stats

    stats = await tool_behavior_stats()
    assert stats["search"]["calls"] == 2
    assert stats["search"]["errors"] == 1
    assert stats["search"]["error_rate"] == 0.5
    assert stats["search"]["avg_result_len"] > 0


async def test_tool_behavior_stats_window_excludes_old(db):
    await _l0_tool(_use("t1", "old_tool", "старый вызов"), "tool_use", ts=1_000_000_000.0)
    await _l0_tool(_result("t1", "старый результат"), "tool_result", ts=1_000_000_000.0)
    await _l0_tool(_use("t2", "new_tool", "свежий вызов"), "tool_use")
    await _l0_tool(_result("t2", "свежий результат"), "tool_result")

    from lifecycle.tool_stats import tool_behavior_stats

    stats = await tool_behavior_stats(days=30)
    assert set(stats) == {"new_tool"}
