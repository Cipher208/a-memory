#!/usr/bin/env python3
"""Phase H Task 4: ariel-cli — thin argparse over the query DSL (read-only).

Subcommands:
    ls               list wiki pages (title / type / status) for a layer
    tree             MOC-style hierarchy: wiki pages grouped by type with counts
    find QUERY       query_dsl content search over core_memory/episodes (top 20)
    grep PATTERN     LIKE search over core_memory values + wiki content → file/key
    stats            per-table counts: l0 statuses, core, episodes, wiki, epi graph

MCP_MEMORY_DATA_DIR is read by shared.connection at import time (l0_cli
pattern): set it in the environment BEFORE running. Without it the default
data dir is used. Missing/unmigrated database fails cleanly with exit 1.

Usage:
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/ariel_cli.py ls --layer agent
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/ariel_cli.py tree
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/ariel_cli.py find deploy
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/ariel_cli.py grep rollback
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/ariel_cli.py stats
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

FIND_LIMIT = 20
GREP_LIMIT = 50


async def _with_db(op: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Close aiosqlite connections so the CLI process exits (l0_cli pattern)."""
    try:
        return await op()
    finally:
        from shared.connection import connection_manager

        await connection_manager.close_all()


async def _ls(layer: str, limit: int) -> dict[str, Any]:
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    rows = await (
        await conn.execute(
            "SELECT title, wiki_type, status FROM wiki_index WHERE layer=? ORDER BY updated_at DESC LIMIT ?",
            (layer, limit),
        )
    ).fetchall()
    return {"pages": [dict(r) for r in rows], "layer": layer, "count": len(rows)}


async def _tree(layer: str) -> dict[str, Any]:
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    rows = await (
        await conn.execute(
            "SELECT wiki_type, title, file_path FROM wiki_index WHERE layer=? ORDER BY wiki_type, updated_at DESC",
            (layer,),
        )
    ).fetchall()
    tree: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        tree.setdefault(str(r["wiki_type"]), []).append({"title": str(r["title"]), "file_path": str(r["file_path"])})
    return {"tree": tree, "layer": layer}


async def _find(user_id: str, layer: str, source: str, query: str) -> dict[str, Any]:
    from features.query_dsl import query_memory

    return await query_memory(user_id=user_id, layer=layer, source=source, content_like=query, limit=FIND_LIMIT)


async def _grep(pattern: str) -> dict[str, Any]:
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    like = f"%{pattern}%"
    conn = await connection_manager.get(DB_NAME)
    core_rows = await (await conn.execute("SELECT key FROM core_memory WHERE value LIKE ? LIMIT ?", (like, GREP_LIMIT))).fetchall()
    wiki_rows = await (await conn.execute("SELECT file_path FROM wiki_index WHERE content LIKE ? LIMIT ?", (like, GREP_LIMIT))).fetchall()
    return {"core_keys": [str(r["key"]) for r in core_rows], "wiki_paths": [str(r["file_path"]) for r in wiki_rows]}


async def _stats() -> dict[str, Any]:
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)

    async def _count(sql: str) -> int:
        row = await (await conn.execute(sql)).fetchone()
        return int(row[0]) if row else 0

    status_rows = await (await conn.execute("SELECT status, COUNT(*) AS n FROM l0_journal GROUP BY status")).fetchall()
    return {
        "l0_journal": {str(r["status"]): int(r["n"]) for r in status_rows},
        "core_memory": await _count("SELECT COUNT(*) FROM core_memory"),
        "episodes": await _count("SELECT COUNT(*) FROM episodes"),
        "wiki_index": await _count("SELECT COUNT(*) FROM wiki_index"),
        "epi_nodes": await _count("SELECT COUNT(*) FROM epi_nodes"),
        "epi_edges": await _count("SELECT COUNT(*) FROM epi_edges"),
    }


def _cmd_ls(args: argparse.Namespace) -> int:
    res = asyncio.run(_with_db(lambda: _ls(args.layer, args.limit)))
    for p in res["pages"]:
        print(f"{p['title']}\t{p['wiki_type']}\t{p['status']}")
    return 0


def _cmd_tree(args: argparse.Namespace) -> int:
    res = asyncio.run(_with_db(lambda: _tree(args.layer)))
    for wtype, pages in res["tree"].items():
        print(f"{wtype} ({len(pages)})")
        for p in pages:
            print(f"  {p['title']}  [{p['file_path']}]")
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    res = asyncio.run(_with_db(lambda: _find(args.user, args.layer, args.source, args.query)))
    for r in res["rows"]:
        if args.source == "core":
            print(f"[{r['key']}] {str(r['value'])[:160]}")
        else:
            print(f"[{','.join(r['tags'])}] {str(r['summary'])[:160]}")
    return 0


def _cmd_grep(args: argparse.Namespace) -> int:
    res = asyncio.run(_with_db(lambda: _grep(args.pattern)))
    for key in res["core_keys"]:
        print(f"core:{key}")
    for path in res["wiki_paths"]:
        print(f"wiki:{path}")
    return 0


def _cmd_stats(_args: argparse.Namespace) -> int:
    print(json.dumps(asyncio.run(_with_db(_stats)), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ariel-cli — read-only memory introspection (Phase H Task 4)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ls = sub.add_parser("ls", help="list wiki pages (title/type/status) for a layer")
    p_ls.add_argument("--layer", choices=("user", "agent"), default="user", help="wiki layer (default user)")
    p_ls.add_argument("--limit", type=int, default=50, help="max pages (default 50)")
    p_ls.set_defaults(fn=_cmd_ls)

    p_tree = sub.add_parser("tree", help="MOC hierarchy: wiki pages grouped by type with counts")
    p_tree.add_argument("--layer", choices=("user", "agent"), default="user", help="wiki layer (default user)")
    p_tree.set_defaults(fn=_cmd_tree)

    p_find = sub.add_parser("find", help="query_dsl content search (top 20)")
    p_find.add_argument("query", help="content LIKE pattern")
    p_find.add_argument("--layer", choices=("user", "agent"), default="user", help="memory layer (default user)")
    p_find.add_argument("--source", choices=("core", "episodes"), default="core", help="query target (default core)")
    p_find.add_argument("--user", default="default", help="user_id (default 'default')")
    p_find.set_defaults(fn=_cmd_find)

    p_grep = sub.add_parser("grep", help="LIKE search over core_memory + wiki_index (file/key output)")
    p_grep.add_argument("pattern", help="substring to search for")
    p_grep.set_defaults(fn=_cmd_grep)

    p_stats = sub.add_parser("stats", help="l0 statuses + core/episodes/wiki/epi-graph counts (JSON)")
    p_stats.set_defaults(fn=_cmd_stats)

    args = ap.parse_args(argv)
    try:
        rc: int = args.fn(args)
    except Exception as exc:  # clean failure: missing/unmigrated DB, legacy schema
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
