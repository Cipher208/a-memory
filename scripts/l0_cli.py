#!/usr/bin/env python3
"""Task F8: L0 pipeline CLI — thin wrapper over features/replay + l0_sweep + stats.

Usage:
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/l0_cli.py replay --since 7 --gate g1
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/l0_cli.py sweep
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory python scripts/l0_cli.py stats

MCP_MEMORY_DATA_DIR is read by shared.connection at import time (backup_cron
pattern): set it in the environment BEFORE running. Without it the default
data dir is used. A missing/unmigrated database fails cleanly with exit 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

AGE_BUCKETS = (("<1h", 3600), ("1h-1d", 86400), ("1d-7d", 7 * 86400))


def _cmd_replay(args: argparse.Namespace) -> int:
    from features.replay import replay

    res = asyncio.run(_with_db(lambda: replay(since_days=args.since, gate=args.gate)))
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    from lifecycle.l0_sweep import sweep_expired

    res = asyncio.run(_with_db(lambda: sweep_expired(min_remain=args.min_remain)))
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_stats(_args: argparse.Namespace) -> int:
    import time

    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    async def _run() -> dict:
        conn = await connection_manager.get(DB_NAME)
        statuses = {
            r["status"]: r["n"]
            for r in await (await conn.execute("SELECT status, COUNT(*) AS n FROM l0_journal GROUP BY status ORDER BY n DESC")).fetchall()
        }
        now = time.time()
        ages: dict[str, int] = {"<1h": 0, "1h-1d": 0, "1d-7d": 0, ">7d": 0}
        for r in await (await conn.execute("SELECT ts FROM l0_journal")).fetchall():
            age = now - float(r["ts"])
            for name, limit in AGE_BUCKETS:
                if age < limit:
                    ages[name] += 1
                    break
            else:
                ages[">7d"] += 1
        return {"statuses": statuses, "age": ages}

    print(json.dumps(asyncio.run(_with_db(_run)), ensure_ascii=False))
    return 0


async def _with_db(op):
    """Run op with the connection_manager, then close connections.

    Aiosqlite worker threads would otherwise keep the CLI process alive
    after output.
    """
    try:
        return await op()
    finally:
        from shared.connection import connection_manager

        await connection_manager.close_all()


def main() -> int:
    ap = argparse.ArgumentParser(description="L0 pipeline CLI (Task F8)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_replay = sub.add_parser("replay", help="re-run G1 distiller over the l0_journal window")
    p_replay.add_argument("--since", type=int, default=7, metavar="N", help="window in days (default 7)")
    p_replay.add_argument("--gate", default="g1", help="gate id recorded in decisions (default g1)")
    p_replay.set_defaults(fn=_cmd_replay)

    p_sweep = sub.add_parser("sweep", help="delete expired L4 rows (B5 protections)")
    p_sweep.add_argument("--min-remain", type=int, default=50, help="never sweep below this many rows (default 50)")
    p_sweep.set_defaults(fn=_cmd_sweep)

    p_stats = sub.add_parser("stats", help="L0 status counts + age distribution")
    p_stats.set_defaults(fn=_cmd_stats)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except Exception as exc:  # clean failure: missing/unmigrated DB, bad gate, ...
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
