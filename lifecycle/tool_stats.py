"""Per-tool behavior statistics (C3/S6b): calls, errors, error_rate, avg_result_len.

The source is l0_journal (raw_type='tool_use'/'tool_result'), joined on
tool_use_id: tool_use.id ↔ tool_result.tool_use_id (Claude-format blocks:
[{"type":"tool_use","id":..,"name":..,"input":{..}}] and
[{"type":"tool_result","tool_use_id":..,"content":..,"is_error":..}];
single dict blocks are parsed too. No LLM calls.
"""

from __future__ import annotations

import json
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

_SNIP = 200  # a graph node must not be a wall of text

_TOOL_TYPES = ("tool_use", "tool_result")


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


async def scan_tool_pairs(
    conn: Any,
    since_ts: float,
    layer: str | None = None,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, int]]:
    """Return (pairs of (use_block, result_block) matched by tool_use_id, calls per tool name).

    l0_journal rows with raw_type tool_use/tool_result and ts >= since_ts
    (layer — a filter, None = all), ordered by ts; uses without a result
    (dangling) do not form pairs. use_block carries _uid/_layer from the row.
    """
    where, params = "raw_type IN ('tool_use','tool_result') AND ts >= ?", [since_ts]
    if layer is not None:
        where += " AND layer = ?"
        params.append(layer)  # type: ignore[arg-type]  # sqlite placeholders: float and str in one list
    rows = await (await conn.execute(f"SELECT layer, user_id, text FROM l0_journal WHERE {where} ORDER BY ts", params)).fetchall()
    uses: dict[str, dict[str, Any]] = {}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    calls: dict[str, int] = {}
    for r in rows:
        for block in parse_tool_blocks(str(r["text"])):
            if block["type"] == "tool_use":
                name = str(block.get("name") or "unknown")
                calls[name] = calls.get(name, 0) + 1
                tid = str(block.get("id") or "")
                if tid:
                    uses[tid] = {**block, "_uid": str(r["user_id"]), "_layer": str(r["layer"])}
            else:
                use = uses.get(str(block.get("tool_use_id") or ""))
                if use is not None:
                    pairs.append((use, block))
    return pairs, calls


async def tool_behavior_stats(*, days: int = 30) -> dict[str, dict[str, float]]:
    """Per-tool: calls, errors, error_rate, avg_result_len over the days window → JSON."""
    conn = await connection_manager.get(DB_NAME)
    pairs, calls = await scan_tool_pairs(conn, time.time() - days * 86400.0)
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
