"""Per-tool behavior statistics (C3/S6b): calls, errors, error_rate, avg_result_len.

The source is l0_journal (raw_type='tool_use'/'tool_result'), joined on
tool_use_id: tool_use.id ↔ tool_result.tool_use_id (Claude-format blocks:
[{"type":"tool_use","id":..,"name":..,"input":{..}}] and
[{"type":"tool_result","tool_use_id":..,"content":..,"is_error":..}]);
single dict blocks are parsed too. No LLM calls.

The matching has exactly ONE owner: `pair_tool_rows`. Two read paths need it
— the dispatch read of `scan_tool_pairs` and the connection-bound read of
`read_tool_pairs_on` that a miner uses (it must see the snapshot it writes
on) — and both go through the same matcher and the same query builder, so a
fix to the matching applies to both.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch, get_write_dispatch

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    #: One l0_journal row as the tool-pair scan reads it: the columns the
    #: query selects, by name.
    ToolRow = Mapping[str, Any]

_SNIP = 200  # a graph node must not be a wall of text

_TOOL_TYPES = ("tool_use", "tool_result")

#: The l0_journal columns the tool-pair scan reads.
_TOOL_PAIR_COLUMNS = "layer, user_id, text"


def parse_tool_blocks(text: str) -> list[dict[str, Any]]:
    """Return tool_use/tool_result blocks from text (a JSON array, a single dict, or [])."""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return []
    if isinstance(obj, dict):
        obj = [obj]
    if not isinstance(obj, list):
        return []
    return [b for b in obj if isinstance(b, dict) and b.get("type") in _TOOL_TYPES]


def tool_result_text(content: Any) -> str:
    """Turn tool_result content into text (a string or [{'type':'text','text':...}])."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b["text"] if isinstance(b, dict) and isinstance(b.get("text"), str) else b for b in content if isinstance(b, (dict, str))]
        return " ".join(str(p) for p in parts).strip()
    return ""


def tool_query_text(tool_input: Any) -> str:
    """Extract the main string argument of tool_use.input (query/pattern/command → first line)."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("query", "pattern", "command", "q"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return next((v.strip() for v in tool_input.values() if isinstance(v, str) and v.strip()), "")


def tool_pair_query(since_ts: float, layer: str | None = None) -> tuple[str, tuple[Any, ...]]:
    """Build the l0_journal query for the tool_use/tool_result rows of the window.

    One builder for both read paths: the SQL they run — the columns, the
    window, the layer filter, the order — cannot drift apart.
    """
    where, params = "raw_type IN ('tool_use','tool_result') AND ts >= ?", [since_ts]
    if layer is not None:
        where += " AND layer = ?"
        params.append(layer)  # type: ignore[arg-type]  # sqlite placeholders: float and str in one list
    sql = f"SELECT {_TOOL_PAIR_COLUMNS} FROM l0_journal WHERE {where} ORDER BY ts"
    return sql, tuple(params)


def pair_tool_rows(rows: Sequence[ToolRow], layer: str | None = None) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, int]]:
    """Match the tool_use blocks of the rows to their tool_result → (pairs, calls per tool).

    The ONE owner of the matching: a tool_use block is paired with the
    tool_result that carries its `tool_use_id`. Uses without a result
    (dangling) do not form pairs, and a result without a use does not either.
    Every tool_use block counts as one call of its tool, paired or not.

    The rows are read by the caller, so this works on a dispatch read and on a
    miner's own connection alike — one matching for both read paths. `layer`
    filters the rows by their layer (the query filters them too; the matcher
    repeats the filter so the two paths cannot disagree on it).
    """
    uses: dict[str, dict[str, Any]] = {}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    calls: dict[str, int] = {}
    for row in rows:
        row_layer = str(row["layer"])
        if layer is not None and row_layer != layer:
            continue
        for block in parse_tool_blocks(str(row["text"])):
            if block["type"] == "tool_use":
                name = str(block.get("name") or "unknown")
                calls[name] = calls.get(name, 0) + 1
                tid = str(block.get("id") or "")
                if tid:
                    uses[tid] = {**block, "_uid": str(row["user_id"]), "_layer": row_layer}
            else:
                use = uses.get(str(block.get("tool_use_id") or ""))
                if use is not None:
                    pairs.append((use, block))
    return pairs, calls


async def scan_tool_pairs(
    dispatch: WriteDispatch,
    since_ts: float,
    layer: str | None = None,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, int]]:
    """Return (pairs of (use_block, result_block) matched by tool_use_id, calls per tool name).

    l0_journal rows with raw_type tool_use/tool_result and ts >= since_ts
    (layer — a filter, None = all), ordered by ts; uses without a result
    (dangling) do not form pairs. use_block carries _uid/_layer from the row.

    The read goes through the write dispatch, not through the shared handle:
    the dispatch opens, reads and closes the cursor inside one method body, so
    no read snapshot is pinned on a connection the whole process shares (a
    pinned snapshot turns the next write on that handle into BUSY_SNAPSHOT 517).
    """
    sql, params = tool_pair_query(since_ts, layer)
    rows = await dispatch.query(DB_NAME, sql, params)
    return pair_tool_rows(rows, layer)


async def read_tool_pairs_on(
    conn: Any,
    since_ts: float,
    layer: str | None = None,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, int]]:
    """Read the tool pairs on `conn` — one connection, one snapshot.

    For a miner that must read what it writes: the dispatch reads on its own
    connection, on another snapshot, so a journal row the caller inserted but
    has not committed is invisible to it. The matching is the shared one
    (`pair_tool_rows`), so this path and the dispatch path cannot disagree.
    """
    sql, params = tool_pair_query(since_ts, layer)
    cur = await conn.execute(sql, params)
    try:
        rows = await cur.fetchall()
    finally:
        await cur.close()
    return pair_tool_rows(rows, layer)


async def tool_behavior_stats(*, days: int = 30) -> dict[str, dict[str, float]]:
    """Per-tool: calls, errors, error_rate, avg_result_len over the days window → JSON."""
    dispatch = get_write_dispatch(connection_manager)
    pairs, calls = await scan_tool_pairs(dispatch, time.time() - days * 86400.0)
    stats: dict[str, dict[str, float]] = {
        name: {"calls": float(n), "errors": 0.0, "error_rate": 0.0, "avg_result_len": 0.0} for name, n in calls.items()
    }
    lens: dict[str, list[int]] = {}
    for use, result in pairs:
        name = str(use.get("name") or "unknown")
        s = stats.setdefault(name, {"calls": 0.0, "errors": 0.0, "error_rate": 0.0, "avg_result_len": 0.0})
        if result.get("is_error"):
            s["errors"] += 1
        lens.setdefault(name, []).append(len(tool_result_text(result.get("content"))))
    for name, s in stats.items():
        if s["calls"]:
            s["error_rate"] = s["errors"] / s["calls"]
        if lens.get(name):
            s["avg_result_len"] = sum(lens[name]) / len(lens[name])
    return stats
