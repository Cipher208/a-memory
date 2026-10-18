"""Bulk operations on typed memory.

Usage:
    python -m features.typed_export export --user alice --kind instruction
    python -m features.typed_export reclassify --user alice            # preview (default)
    python -m features.typed_export reclassify --user alice --apply    # write
    python -m features.typed_export backfill --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path  # noqa: TC003 — do_export return type
from typing import TYPE_CHECKING, Any, TypeVar

from lifecycle.importance_scheduler import ImportanceScheduler
from shared.connection import connection_manager
from shared.write_dispatch import get_write_dispatch
from shared.memory_types import backfill_null_kinds, kind_for_text

if TYPE_CHECKING:
    from collections.abc import Coroutine


async def do_export(user_id: str, kind: str | None) -> Path:
    """Export typed rows to a JSON file (E4: previously fetched and dropped)."""
    import json
    import time
    from pathlib import Path

    sql = "SELECT * FROM core_memory WHERE user_id=?"
    params: tuple[str, ...] = (user_id,)
    if kind:
        sql += " AND memory_kind=?"
        params = (user_id, kind)
    rows = await get_write_dispatch().query("memory.db", sql, params)

    base = Path(str(connection_manager.base_dir)) / "exports"
    base.mkdir(parents=True, exist_ok=True)
    suffix = f"_{kind}" if kind else ""
    out = base / f"typed_export_{user_id}{suffix}_{int(time.time())}.json"
    payload = {
        "user_id": user_id,
        "memory_kind": kind,
        "exported_at": time.time(),
        "rows": [dict(r) for r in rows],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


async def do_reclassify(user_id: str, dry_run: bool = True) -> list[tuple[str, str, int]]:
    """Re-derive `memory_kind` from the text, for the rows the text still decides.

    Two safeguards, both read off the store rather than invented here.

    DECLARED KINDS ARE SKIPPED. Some rows do not have a derived kind at all: the
    identity mirror passes `--kind` explicitly, and `think(kind=...)` and the
    persona-owner hook write a `canon:<kind>:` key. Re-deriving those would replace
    authored truth with a content guess — the same protection the scheduler already
    gives their importance through `CURATED_SOURCES` (E1 18.09), reused rather than
    copied so the two cannot drift apart.

    THE PREVIEW IS THE DEFAULT. This rewrites kinds in bulk, and the previous shape
    built its change list and returned it silently, so a caller had no way to see the
    plan before it landed. `dry_run` now prints it and writes nothing; `--apply` is
    the explicit write.

    Returns the plan as `(old_kind, new_kind, entry_id)` triples, in both modes, so a
    caller can assert on it without parsing stdout.
    """
    rows = await get_write_dispatch().query(
        "memory.db",
        "SELECT entry_id as id, key, value, memory_kind, source FROM core_memory WHERE user_id=?",
        (user_id,),
    )
    changes: list[tuple[str, str, int]] = []
    keys: dict[int, str] = {}
    curated = 0
    for r in rows:
        if str(r["source"] or "") in ImportanceScheduler.CURATED_SOURCES:
            curated += 1
            continue
        new_kind = kind_for_text(r["value"]).value
        if r["memory_kind"] != new_kind:
            changes.append((str(r["memory_kind"]), new_kind, int(r["id"])))
            keys[int(r["id"])] = str(r["key"] or "")

    verb = "would change" if dry_run else "changed"
    print(
        f"reclassify: {len(changes)} of {len(rows)} rows {verb}"
        f" ({curated} curated rows kept their declared kind)"
        f"{' — dry run, nothing written (pass --apply to write)' if dry_run else ''}"
    )
    for old, new, rid in changes[:50]:
        print(f"  {rid}  {old} -> {new}  {keys[rid][:60]}")
    if len(changes) > 50:
        print(f"  ... and {len(changes) - 50} more")

    if dry_run or not changes:
        return changes
    await get_write_dispatch().write_many(
        "memory.db",
        [("UPDATE core_memory SET memory_kind=? WHERE entry_id=?", (kind, rid)) for _old, kind, rid in changes],
    )
    return changes


async def do_backfill(dry_run: bool) -> None:
    await backfill_null_kinds(connection_manager, dry_run=dry_run)


T = TypeVar("T")


def _run(op: Coroutine[Any, Any, T]) -> T:
    """Run one subcommand, then close the aiosqlite workers.

    aiosqlite's worker threads are non-daemon and block in `queue.get()`, so a CLI that
    leaves a connection open finishes its work and then hangs at interpreter shutdown.
    `scripts/ariel_cli.py::_with_db` closes that door for the CLI's own entry points;
    this module is also run directly, so it has to close its own.
    """

    async def _wrapped() -> T:
        try:
            return await op
        finally:
            await connection_manager.close_all()

    return asyncio.run(_wrapped())


def main() -> None:
    p = argparse.ArgumentParser(description="Bulk operations on typed memory")
    sp = p.add_subparsers(dest="cmd", required=True)

    e = sp.add_parser("export")
    e.add_argument("--user", required=True)
    e.add_argument("--kind")

    r = sp.add_parser("reclassify")
    r.add_argument("--user", required=True)
    r.add_argument("--apply", action="store_true", help="write the plan (default: preview only)")
    r.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)  # kept: it is now the default

    b = sp.add_parser("backfill")
    b.add_argument("--dry-run", action="store_true")

    args = p.parse_args()

    if args.cmd == "export":
        _run(do_export(args.user, args.kind))
    elif args.cmd == "reclassify":
        _run(do_reclassify(args.user, dry_run=not args.apply))
    elif args.cmd == "backfill":
        _run(do_backfill(args.dry_run))


if __name__ == "__main__":
    main()
