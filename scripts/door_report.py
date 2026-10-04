#!/usr/bin/env python3
"""Read the intake door's refusal counter — what the guards are throwing away.

USAGE

    .venv/bin/python3 scripts/door_report.py                     # this base
    .venv/bin/python3 scripts/door_report.py --data-dir DIR
    .venv/bin/python3 scripts/door_report.py --days 7 --limit 40
    .venv/bin/python3 scripts/door_report.py --reason transcript

WHY THIS EXISTS

`memory_dispatch_log` records what was SAVED, and nothing recorded what was
REFUSED. Every guard in `auto_save_text` and every block in the middleware
pipeline returns before that insert, so a refused message left no row anywhere.
Measured before this counter existed, one live base held 1419 dispatch rows and
not one with `score = 0.0`.

That silence cost real data: the intake guard's markdown class ate 624 of one
persona's 790 messages — 79.7% of her voice — and the loss was findable only by
re-deriving the number from the source database by hand. Here it is a rate the
operator can see: one or two refusals a day, then hundreds in a single window.

HOW TO READ THE OUTPUT

`below_importance_threshold` is the designed filter working — not a defect.
`duplicate_l0_block` is a replay. The reasons that mean a guard may be wrong are
`transcript`, `harness_limit` and `system_injection`: each one is a judgement
about the TEXT, and a wrong judgement there deletes a message silently. So this
script prints previews, not just counts — a count says something changed, the
preview says whether the guard was right.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

#: Reasons worth a human's attention: each is a text judgement that can be wrong.
SUSPECT = {"transcript", "harness_limit", "system_injection"}


async def _report(data_dir: Path, days: float, limit: int, reason: str) -> int:
    from shared.connection import connection_manager
    from shared.constants import DB_NAME
    from shared.door_log import recent_rejections, rejection_summary

    original = connection_manager.base_dir
    connection_manager.base_dir = data_dir
    try:
        since = time.time() - days * 86400 if days > 0 else None
        summary = await rejection_summary(since=since)
        if not summary["available"]:
            print(f"  no rejection log at {data_dir / DB_NAME}")
            print("  (pre-migration base, or the table was never created — run alembic upgrade head)")
            return 1

        window = f"last {days:g}d" if days > 0 else "all time"
        print(f"── refusals at {data_dir.name} ({window}) " + "─" * 20)
        print(f"  total: {summary['total']}")
        if not summary["total"]:
            print("  nothing refused — the door is not eating anything")
            return 0

        print("\n  by reason:")
        for why, count in sorted(summary["by_reason"].items(), key=lambda kv: -kv[1]):
            mark = "  <-- check this" if why in SUSPECT else ""
            print(f"    {count:>7}  {why}{mark}")

        print("\n  by layer:")
        for layer, count in sorted(summary["by_layer"].items(), key=lambda kv: -kv[1]):
            print(f"    {count:>7}  {layer}")

        suspects = {r: c for r, c in summary["by_reason"].items() if r in SUSPECT}
        if suspects:
            print(
                "\n  A text-judging guard refused something. Below are the newest previews:\n"
                "  if these look like the persona's own words, the guard is wrong, not the message."
            )
        recent = await recent_rejections(limit=limit)
        if reason:
            recent = [r for r in recent if r["reason"] == reason]
        print(f"\n  newest refusals ({len(recent)} of {limit}):")
        for r in recent:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"] or 0))
            src = r["source_msg_id"] if r["source_msg_id"] is not None else "-"
            print(f"    [{stamp}] {r['reason']:<26} {r['layer']:<6} src={src}")
            print(f"        {r['preview'][:110]!r}")
        return 0
    finally:
        connection_manager.base_dir = original


def main() -> int:
    ap = argparse.ArgumentParser(description="Show what the intake door refused.")
    ap.add_argument("--data-dir", default="", help="agent data dir (default: $MCP_DATA_DIR or ~/.mcp-ariel-memory)")
    ap.add_argument("--days", type=float, default=0, help="window in days; 0 means all time (default: 0)")
    ap.add_argument("--limit", type=int, default=20, help="how many previews to print (default: 20)")
    ap.add_argument("--reason", default="", help="show previews for one reason only")
    args = ap.parse_args()

    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser()
    else:
        import os

        env = os.environ.get("MCP_DATA_DIR", "")
        data_dir = Path(env).expanduser() if env else Path.home() / ".mcp-ariel-memory"
    return asyncio.run(_report(data_dir, args.days, args.limit, args.reason))


if __name__ == "__main__":
    raise SystemExit(main())
