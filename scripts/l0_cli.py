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

    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    # `--ignore-gate` is the window run's escape hatch: the gate applies to a window
    # run because automation calls one every night, and a person who wants the old
    # bypass-the-gate behaviour has to say so. `--ids` already implies it.
    respect_gate = None if args.ignore_gate is None else (not args.ignore_gate)
    res = asyncio.run(
        _with_db(
            lambda: replay(
                since_days=args.since,
                gate=args.gate,
                ids=ids,
                respect_gate=respect_gate,
                max_rows=args.max_rows,
            )
        )
    )
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_close_overdue(args: argparse.Namespace) -> int:
    from lifecycle.l0_tiers import close_overdue_received

    res = asyncio.run(_with_db(lambda: close_overdue_received(ttl_days=args.ttl_days)))
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


def _cmd_verify(_args: argparse.Namespace) -> int:
    from shared.l0 import verify_chain

    broken = asyncio.run(_with_db(verify_chain))
    if not broken:
        print('{"broken": 0, "status": "ok"}')
        return 0
    print(json.dumps({"broken": len(broken), "first": broken[0]}, ensure_ascii=False))
    return 1


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
    p_replay.add_argument(
        "--ids",
        default=None,
        metavar="N,N,...",
        help="replay exactly these journal row ids, ignoring --since and the status filter. Use to wake named rows out of a stranded backlog without distilling the rest of it.",
    )
    p_replay.add_argument(
        "--ignore-gate",
        action="store_true",
        default=None,
        help="distil the window without consulting the importance gate. The gate applies by default because the nightly pass calls replay; a manual window run that wants the old behaviour must ask for it.",
    )
    p_replay.add_argument(
        "--max-rows",
        type=int,
        default=None,
        metavar="N",
        help="upper bound on rows this run processes (default from config l0.replay_max_rows)",
    )
    p_replay.set_defaults(fn=_cmd_replay)

    p_close = sub.add_parser("close-overdue", help="close `received` rows older than the TTL as gated_out/never_processed")
    p_close.add_argument("--ttl-days", type=float, default=None, help="override config l0.received_ttl_days")
    p_close.set_defaults(fn=_cmd_close_overdue)

    p_sweep = sub.add_parser("sweep", help="delete expired L4 rows (B5 protections)")
    p_sweep.add_argument("--min-remain", type=int, default=50, help="never sweep below this many rows (default 50)")
    p_sweep.set_defaults(fn=_cmd_sweep)

    p_stats = sub.add_parser("stats", help="L0 status counts + age distribution")
    p_stats.set_defaults(fn=_cmd_stats)

    p_verify = sub.add_parser("verify", help="recompute L0 hash-chain, report broken rows (exit 1 on tamper)")
    p_verify.set_defaults(fn=_cmd_verify)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except Exception as exc:  # clean failure: missing/unmigrated DB, bad gate, ...
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
