#!/usr/bin/env python3
"""Resolve flagged memory_conflicts through ConflictResolver.resolve().

Operator tool for the ariel-memory base. Verified 2026-09-15: the hermes base
has 53 flagged rows in 53 groups, one row per group. For a single-row group
resolve(gid, keep_id=row.id) deletes nothing, clears the flag on that row and
writes the audit trail. Raw DELETE is deliberately not used: it skips the
archive step and the audit trail.

    python scripts/resolve_conflicts.py            # preview
    python scripts/resolve_conflicts.py --apply    # execute
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DATA_DIR = os.environ.get("MCP_MEMORY_DATA_DIR", str(Path.home() / ".mcp-ariel-memory-hermes"))
os.environ["MCP_MEMORY_DATA_DIR"] = DATA_DIR
os.environ.setdefault("MCP_CONFIG_PATH", str(Path(DATA_DIR) / "config.yaml"))


async def flagged(conn) -> list:
    cur = await conn.execute("SELECT id, conflict_group_id, substr(content,1,60) FROM memory_conflicts WHERE is_conflict=1 ORDER BY id")
    return list(await cur.fetchall())


async def run(apply: bool, limit: int = 0) -> int:
    from rag.conflict import ConflictResolver
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    cm = connection_manager
    rows = await flagged(await cm.get(DB_NAME))
    if limit:
        rows = rows[:limit]
    print(f"base={DATA_DIR}  flagged={len(rows)}  mode={'APPLY' if apply else 'dry-run'}")

    resolver = ConflictResolver(cm=cm)
    ok = fail = 0
    for rid, gid, preview in rows:
        if not gid:
            print(f"  SKIP id={rid}: no conflict_group_id")
            fail += 1
            continue
        if not apply:
            print(f"  dry  id={rid} gid={gid[:8]}  {preview[:50]!r}")
            continue
        try:
            if await resolver.resolve(gid, rid):
                ok += 1
                print(f"  ok   id={rid} gid={gid[:8]}")
            else:
                fail += 1
                print(f"  FAIL id={rid} gid={gid[:8]} (resolve returned False)")
        except Exception as e:  # one row must not stop the batch
            fail += 1
            print(f"  ERR  id={rid}: {type(e).__name__}: {e}")

    left = len(await flagged(await cm.get(DB_NAME)))
    print(f"done: resolved={ok} failed={fail}  flagged_before={len(rows)} flagged_after={left}")
    if apply and not limit:
        assert left == len(rows) - ok, f"flag count mismatch: {left} != {len(rows) - ok}"
    await cm.close_all()
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: preview only)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows")
    a = ap.parse_args()
    return asyncio.run(run(a.apply, a.limit))


if __name__ == "__main__":
    raise SystemExit(main())
