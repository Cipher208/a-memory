"""The tool_use/tool_result matching must have exactly ONE owner.

`lifecycle.tool_stats.scan_tool_pairs` reads the L0 journal through the write
dispatch; `lifecycle.graph_miners._scan_tool_pairs_on` reads it on the
miner's own connection, because a miner must read what it writes (the
borrowed branch of `_maintenance` writes on the shared handle, and a read
through the dispatch is a read on another connection, on another snapshot).

Both read the same rows and match the same blocks, so the matching is shared:
one matcher (`lifecycle.tool_stats.pair_tool_rows`) and one query builder
(`lifecycle.tool_stats.tool_pair_query`).

Two copies of that matching are a drift hazard — a fix to one silently does
not apply to the other — so these tests pin:

* the two read paths produce IDENTICAL pairs and IDENTICAL per-tool call
  counts for the same rows, a dangling use without a result and the layer
  filter included;
* the miner's own entry point agrees with the dispatch read;
* no second implementation of the matching exists in `lifecycle/`.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager
from shared.write_dispatch import get_write_dispatch

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def db(tmp_path):
    """A migrated base in a tmp dir — the real `~/.mcp-ariel-memory` is never touched."""
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


def _use(tid: str, name: str, query: str) -> str:
    return json.dumps([{"type": "tool_use", "id": tid, "name": name, "input": {"query": query}}])


def _result(tid: str, content: str) -> str:
    return json.dumps([{"type": "tool_result", "tool_use_id": tid, "content": content}])


async def _l0_tool(text: str, raw_type: str, *, layer: str = "user", ts: float | None = None) -> None:
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type) VALUES (?, 'new_message', NULL, ?, 'gu', ?, ?)",
        (ts if ts is not None else time.time(), layer, text, raw_type),
    )
    await conn.commit()


async def _seed_mixed_rows() -> None:
    """A paired call, a dangling use, an orphan result and a call in another layer."""
    await _l0_tool(_use("t1", "search", "первый вызов"), "tool_use")
    await _l0_tool(_result("t1", "результат первого вызова"), "tool_result")
    # A use without a result: no pair, but the call is still counted.
    await _l0_tool(_use("t2", "search", "висячий вызов без результата"), "tool_use")
    # A result without a use: no pair, and no call.
    await _l0_tool(_result("t8", "результат без вызова"), "tool_result")
    # Another layer: the layer filter must hide it from both read paths.
    await _l0_tool(_use("t3", "grep", "вызов в другом слое"), "tool_use", layer="agent")
    await _l0_tool(_result("t3", "результат в другом слое"), "tool_result", layer="agent")


async def test_the_dispatch_read_and_the_connection_read_agree(db):
    """Both read paths must produce the same pairs and the same call counts.

    The dispatch read (`scan_tool_pairs`) and the connection read
    (`read_tool_pairs_on`) run the same matcher over the same rows. If they
    ever disagree, one of them is wrong — and the miner that reads on its own
    connection is the one that would silently mine a different set of calls.
    """
    await _seed_mixed_rows()

    from lifecycle.tool_stats import read_tool_pairs_on, scan_tool_pairs

    dispatch = get_write_dispatch(connection_manager)
    conn = await connection_manager.get(DB_NAME)

    pairs_dispatch, calls_dispatch = await scan_tool_pairs(dispatch, 0.0, layer="user")
    pairs_conn, calls_conn = await read_tool_pairs_on(conn, 0.0, layer="user")

    assert pairs_dispatch == pairs_conn, "the two read paths paired different blocks"
    assert calls_dispatch == calls_conn, "the two read paths counted different calls"
    # The values themselves: one pair, the dangling use counted, the other layer out.
    assert calls_dispatch == {"search": 2}, calls_dispatch
    assert len(pairs_dispatch) == 1, pairs_dispatch
    assert pairs_dispatch[0][0].get("id") == "t1", pairs_dispatch
    assert pairs_dispatch[0][1].get("tool_use_id") == "t1", pairs_dispatch


async def test_the_dispatch_read_and_the_connection_read_agree_without_a_layer_filter(db):
    """The layer filter is a filter of the query AND of the matcher — both paths apply it."""
    await _seed_mixed_rows()

    from lifecycle.tool_stats import read_tool_pairs_on, scan_tool_pairs

    dispatch = get_write_dispatch(connection_manager)
    conn = await connection_manager.get(DB_NAME)

    pairs_dispatch, calls_dispatch = await scan_tool_pairs(dispatch, 0.0)
    pairs_conn, calls_conn = await read_tool_pairs_on(conn, 0.0)

    assert pairs_dispatch == pairs_conn
    assert calls_dispatch == calls_conn
    assert calls_dispatch == {"search": 2, "grep": 1}, calls_dispatch
    assert len(pairs_dispatch) == 2, pairs_dispatch


async def test_the_miner_reads_the_pairs_the_dispatcher_reads(db):
    """The miner's own entry point must be the shared matcher, not a copy of it."""
    await _seed_mixed_rows()

    from lifecycle.graph_miners import _scan_tool_pairs_on
    from lifecycle.tool_stats import scan_tool_pairs

    dispatch = get_write_dispatch(connection_manager)
    conn = await connection_manager.get(DB_NAME)

    pairs_dispatch, _calls = await scan_tool_pairs(dispatch, 0.0, layer="user")
    pairs_miner = await _scan_tool_pairs_on(conn, 0.0, layer="user")

    assert pairs_miner == pairs_dispatch, "the miner paired different blocks than the dispatcher"


def _pairing_implementations() -> list[tuple[str, str]]:
    """Every function in `lifecycle/` that matches tool_use blocks to their result.

    A pairing implementation is a function that reads the `tool_use_id` key
    AND parses the tool blocks. There must be exactly one of them.
    """
    found: list[tuple[str, str]] = []
    for path in sorted((ROOT / "lifecycle").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            constants = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            calls = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if "tool_use_id" in constants and "parse_tool_blocks" in calls:
                found.append((path.name, node.name))
    return found


def test_the_matching_has_exactly_one_implementation():
    """One owner of the pairing: a second copy is a drift hazard.

    A fix to the matching must apply to both read paths. Two copies of the
    same loop are silent about that: the copy that is not fixed keeps the old
    behaviour, and the two paths start to disagree.
    """
    found = _pairing_implementations()

    assert found == [("tool_stats.py", "pair_tool_rows")], f"the tool pairing has more than one owner: {found}"


def test_the_miner_module_does_not_parse_tool_blocks():
    """`graph_miners.py` must not read the tool blocks itself."""
    source = (ROOT / "lifecycle" / "graph_miners.py").read_text(encoding="utf-8")

    assert "parse_tool_blocks" not in source, "the miner module parses the tool blocks on its own"


def test_the_public_scanner_keeps_its_shape():
    """`scan_tool_pairs` keeps its signature and its return shape."""
    import inspect

    from lifecycle.tool_stats import scan_tool_pairs

    parameters = list(inspect.signature(scan_tool_pairs).parameters)
    assert parameters == ["dispatch", "since_ts", "layer"], parameters


async def test_the_readers_return_the_same_shape(db):
    """Both readers return (pairs, calls) — the miner path just drops the calls."""
    await _seed_mixed_rows()

    from lifecycle.tool_stats import read_tool_pairs_on, scan_tool_pairs

    dispatch = get_write_dispatch(connection_manager)
    conn = await connection_manager.get(DB_NAME)

    dispatch_result = await scan_tool_pairs(dispatch, 0.0, layer="user")
    conn_result = await read_tool_pairs_on(conn, 0.0, layer="user")

    for result in (dispatch_result, conn_result):
        assert isinstance(result, tuple) and len(result) == 2, result
        pairs, calls = result
        assert isinstance(pairs, list) and isinstance(calls, dict), result
        assert all(isinstance(p, tuple) and len(p) == 2 for p in pairs), result
        assert all(isinstance(v, int) for v in calls.values()), result


def test_the_pairing_helper_is_connection_independent():
    """`pair_tool_rows` works on rows, not on a connection: it is testable in isolation."""
    from lifecycle.tool_stats import pair_tool_rows

    rows: list[dict[str, Any]] = [
        {"layer": "user", "user_id": "gu", "text": _use("t1", "search", "q")},
        {"layer": "user", "user_id": "gu", "text": _result("t1", "r")},
    ]

    pairs, calls = pair_tool_rows(rows)

    assert calls == {"search": 1}, calls
    assert len(pairs) == 1, pairs
