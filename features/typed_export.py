"""Bulk operations on typed memory.

Usage:
    python -m features.typed_export export --user alice --kind instruction
    python -m features.typed_export reclassify --user alice --dry-run
    python -m features.typed_export backfill --dry-run
"""

from __future__ import annotations

import argparse
import asyncio

from shared.connection import connection_manager
from shared.memory_types import backfill_null_kinds, kind_for_text


async def do_export(user_id: str, kind: str | None) -> None:
    conn = await connection_manager.get("memory.db")
    if kind:
        await (
            await conn.execute(
                "SELECT * FROM core_memory WHERE user_id=? AND memory_kind=?",
                (user_id, kind),
            )
        ).fetchall()
    else:
        await (
            await conn.execute(
                "SELECT * FROM core_memory WHERE user_id=?",
                (user_id,),
            )
        ).fetchall()

    return


async def do_reclassify(user_id: str, dry_run: bool) -> None:
    conn = await connection_manager.get("memory.db")
    rows = await (
        await conn.execute(
            "SELECT entry_id as id, value, memory_kind FROM core_memory WHERE user_id=?",
            (user_id,),
        )
    ).fetchall()
    changes = []
    for r in rows:
        new_kind = kind_for_text(r["value"]).value
        if r["memory_kind"] != new_kind:
            changes.append((new_kind, r["id"]))
    if dry_run:
        return
    if changes:
        await conn.execute("BEGIN")
        for kind, rid in changes:
            await conn.execute("UPDATE core_memory SET memory_kind=? WHERE entry_id=?", (kind, rid))
        await conn.commit()


async def do_backfill(dry_run: bool) -> None:
    await backfill_null_kinds(connection_manager, dry_run=dry_run)


def main() -> None:
    p = argparse.ArgumentParser(description="Bulk operations on typed memory")
    sp = p.add_subparsers(dest="cmd", required=True)

    e = sp.add_parser("export")
    e.add_argument("--user", required=True)
    e.add_argument("--kind")

    r = sp.add_parser("reclassify")
    r.add_argument("--user", required=True)
    r.add_argument("--dry-run", action="store_true")

    b = sp.add_parser("backfill")
    b.add_argument("--dry-run", action="store_true")

    args = p.parse_args()

    if args.cmd == "export":
        asyncio.run(do_export(args.user, args.kind))
    elif args.cmd == "reclassify":
        asyncio.run(do_reclassify(args.user, args.dry_run))
    elif args.cmd == "backfill":
        asyncio.run(do_backfill(args.dry_run))


if __name__ == "__main__":
    main()
