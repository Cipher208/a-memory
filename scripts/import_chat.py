#!/usr/bin/env python3
"""Phase H Task 3: import_chat — chat exports → L0 via gates (origin=import).

Supported sources:
    claude       claude-conversations.json: [{uuid, name, messages: [{role, content}]}]
    chatgpt      ChatGPT export: [{title, mapping: {id: {message: {author: {role}, content: {parts: []}}}}}]
    memory-json  [{key, value, ts?}] — generic fact dump
    jsonl        one JSON object per line: {text | message | content, role?}

Every normalized record goes through shared.l0.capture(event='import',
raw_type='import' — import lines are not deterministic, classify_raw would
misread them) and is distilled immediately (direct distill_and_route, score
0.6 fixed — real importance is decided by the kind gates). Original message
timestamps are preserved: parsers extract orig ts (claude: created_at ISO,
chatgpt: create_time epoch, memory-json/jsonl: ts) → capture(ts_override=...);
missing ts → now. The g1 decision
with config_hash is recorded on the row, so the watermark replay never
re-processes imported lines.

Usage:
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/import_chat.py \
        --source claude --file ~/exports/claude-conversations.json --user default [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

Rec = dict[str, Any]  # {"role": str | None, "text": str, "ts": float | None} — ts=None → now при capture


def _to_ts(value: Any) -> float | None:
    """Export timestamp → epoch seconds; None если ts в записи нет."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _content_text(content: Any) -> str:
    """Claude content: str | [{type: 'text', text}]."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p if isinstance(p, str) else str(p.get("text") or p.get("content") or "") for p in content if isinstance(p, str | dict)]
        return " ".join(p for p in parts if p)
    return "" if content is None else str(content)


def parse_claude(data: list[dict[str, Any]]) -> list[Rec]:
    recs: list[Rec] = []
    for conv in data:
        for m in conv.get("messages", []):
            recs.append({"role": m.get("role"), "text": _content_text(m.get("content")), "ts": _to_ts(m.get("created_at"))})
    return recs


def parse_chatgpt(data: list[dict[str, Any]]) -> list[Rec]:
    # ponytail: file-order mapping traversal — ChatGPT exports lack a stable sort key here
    recs: list[Rec] = []
    for conv in data:
        for node in conv.get("mapping", {}).values():
            msg = node.get("message") or {}
            if not msg:  # mapping nodes without a message (branch points)
                continue
            role = (msg.get("author") or {}).get("role")
            if role in ("system", "tool"):
                continue
            parts = (msg.get("content") or {}).get("parts") or []
            text = " ".join(p if isinstance(p, str) else _content_text(p) for p in parts).strip()
            recs.append({"role": role, "text": text, "ts": _to_ts(msg.get("create_time"))})
    return recs


def parse_memory_json(data: list[dict[str, Any]]) -> list[Rec]:
    return [
        {
            "role": None,
            "text": f"{r.get('key')}: {r.get('value')}",
            "ts": _to_ts(r.get("ts")),
        }
        for r in data
    ]


def parse_jsonl(lines: list[str]) -> list[Rec]:
    recs: list[Rec] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        text = obj.get("text") or obj.get("message") or obj.get("content") or ""
        recs.append({"role": obj.get("role"), "text": str(text), "ts": _to_ts(obj.get("ts"))})
    return recs


PARSERS: dict[str, Callable[[Any], list[Rec]]] = {
    "claude": parse_claude,
    "chatgpt": parse_chatgpt,
    "memory-json": parse_memory_json,
    "jsonl": parse_jsonl,
}


def load_records(source: str, path: str) -> list[Rec]:
    if source not in PARSERS:
        raise ValueError(f"unknown source: {source} (expected one of {sorted(PARSERS)})")
    if source == "jsonl":
        with open(path, encoding="utf-8") as f:
            return parse_jsonl(f.readlines())
    with open(path, encoding="utf-8") as f:
        return PARSERS[source](json.load(f))


async def _capture_and_distill(user_id: str, text: str, ts: float | None = None) -> dict[str, int]:
    """Capture → direct distill → watermark-mark the row so replay skips it."""
    import json as _json

    from core import MemoryManager
    from features.replay import config_hash
    from graph.epistemic import EpistemicGraph
    from lifecycle.distiller import distill_and_route
    from shared.connection import connection_manager
    from shared.constants import DB_NAME
    from shared.l0 import capture

    rid = await capture("import", "user", user_id, text, raw_type="import", decisions=[{"gate": "import"}], ts_override=ts)
    assert rid is not None  # capture never raises; None only on infra failure
    conn = await connection_manager.get(DB_NAME)
    mem = MemoryManager(cm=connection_manager).get_layer("user", user_id)
    graph = EpistemicGraph(cm=connection_manager, layer="user")
    route = await distill_and_route(mem, graph, user_id, text, 0.6, event="import")
    # condition-splitting (C4): ConflictResolver hit сохраняет ОБЕ записи
    # (scope=earlier/later) и теперь учитывается в l4_saved — routed, не gated out
    status = "promoted_l4" if route["l4_saved"] else ("saved_l3" if (route["l3_saved"] or route["conflicts"]) else "gated_out")
    decisions = [{"gate": "import"}, {"gate": "g1", "config_hash": config_hash(), "ts": time.time()}]
    await conn.execute(
        "UPDATE l0_journal SET status=?, processed_at=?, decisions=? WHERE id=?",
        (status, time.time(), _json.dumps(decisions, ensure_ascii=False), rid),
    )
    await conn.commit()
    return route


async def import_records(source: str, path: str, user_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Parse, then capture (+ direct distill) each normalized record. Never writes on dry_run."""
    recs = [r for r in load_records(source, path) if r["text"].strip()]
    if dry_run:
        return {
            "source": source,
            "file": path,
            "user_id": user_id,
            "dry_run": True,
            "captured": len(recs),
            "preview": [r["text"][:120] for r in recs[:5]],
        }
    l4 = l3 = 0
    for r in recs:
        route = await _capture_and_distill(user_id, r["text"], r["ts"])
        l4 += route["l4_saved"]
        l3 += route["l3_saved"]
    return {"source": source, "file": path, "user_id": user_id, "captured": len(recs), "l4_saved": l4, "l3_saved": l3}


async def _with_db(op: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Close aiosqlite connections so the CLI process exits (l0_cli pattern)."""
    try:
        return await op()
    finally:
        from shared.connection import connection_manager

        await connection_manager.close_all()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import chat exports into L0 (Phase H Task 3)")
    ap.add_argument("--source", required=True, choices=sorted(PARSERS))
    ap.add_argument("--file", required=True, help="path to the export file")
    ap.add_argument("--user", default="default", help="target user_id (default 'default')")
    ap.add_argument("--dry-run", action="store_true", help="show what would be imported, write nothing")
    args = ap.parse_args(argv)
    try:
        res = asyncio.run(_with_db(lambda: import_records(args.source, args.file, args.user, dry_run=args.dry_run)))
    except Exception as exc:  # clean failure: bad file, bad JSON, unmigrated DB
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
