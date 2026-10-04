#!/usr/bin/env python3
r"""Import an autohooks source into L0 — journal only, no distillation.

WHY THIS IS SEPARATE FROM scripts/import_chat.py

`import_chat.py` distills every record it imports (its docstring says so), because
its purpose is to turn an export into memories. This script does the opposite: it
puts a history back into the raw journal and stops. `shared.l0.capture` writes
status='received' and nothing else consumes that status except
`features.replay.replay()`, which is reachable only from `scripts/l0_cli.py
replay` — no daemon, hook or cron calls it. `lifecycle.l0_tiers` states outright
that "the received status is NEVER archived or truncated", and `segment_l0` is
report-only. So rows written here are stable, permanent, and invisible to recall
until someone deliberately runs the replay.

That property is the whole point. A persona whose history was eaten by an intake
bug can have it back without flooding L3/L4 — their canon stays exactly as it was,
and the decision to distil (and how far back) is deferred instead of made under
pressure.

WHAT IT READS

The source is described by an autohooks agent config, not by flags, so the import
sees exactly the rows the daemon would have seen: same table, same
`json_path`/filter/field mappings. The layer is computed with the same
`dispatch_layer` the live path uses — a persona's own words must land on the agent
layer here too, or the import would recreate the very bug it is repairing.

USAGE

    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory-cowagent \\
        .venv/bin/python3 scripts/l0_import_source.py \\
        --config ~/.config/ariel-autohooks/cowagent.yaml --dry-run

    # then for real (drop --dry-run), and later, to distil any window:
    .venv/bin/python3 scripts/l0_cli.py replay --since 75

Timestamps are preserved (`ts_override`), so a later replay window is meaningful.
Note that `replay --since N` counts days back from TODAY, not from the message
date: importing July history and replaying with `--since 7` would distill nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _count(db_path: Path, table: str) -> int:
    import sqlite3

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return -1


def _layer_counts(db_path: Path) -> dict[str, int]:
    import sqlite3

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT layer, COUNT(*) FROM l0_journal GROUP BY layer").fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _same_path(a: object, b: object) -> bool:
    """Compare two paths by real location (sync: `resolve` is blocking I/O)."""
    import os

    return os.path.realpath(str(a)) == os.path.realpath(str(b))


async def _run(args: argparse.Namespace) -> int:
    from autohooks.config import dispatch_layer, load_config
    from autohooks.source import SqliteSource
    from shared.connection import connection_manager
    from shared.constants import DB_NAME
    from shared.l0 import capture, verify_chain

    cfg = load_config(args.config)

    # The guard that matters most. `connection_manager.base_dir` comes from
    # MCP_MEMORY_DATA_DIR, while the source comes from the config's `data_dir`;
    # when those disagree this script would happily stream one persona's history
    # into another base. That is not hypothetical — a migration run during this
    # work went to the shared base because the variable was named MCP_DATA_DIR
    # instead of MCP_MEMORY_DATA_DIR, and only the printed base_dir caught it.
    if not _same_path(connection_manager.base_dir, cfg.data_dir):
        print("  REFUSING TO WRITE: the target base is not the one in the config.")
        print(f"    config data_dir : {cfg.data_dir}")
        print(f"    active base_dir : {connection_manager.base_dir}")
        print(f"    hint            : MCP_MEMORY_DATA_DIR={cfg.data_dir}")
        return 2

    db_path = Path(cfg.data_dir) / DB_NAME
    if not db_path.exists():
        print(f"  no database at {db_path}")
        return 2

    source = SqliteSource.from_config(cfg)
    max_id = source.max_id()
    start = args.from_id
    stop = min(args.to_id or max_id, max_id)

    print(f"  config     : {args.config}")
    print(f"  source     : {cfg.source.path}")
    print(f"  target base: {cfg.data_dir}")
    print(f"  id window  : {start}..{stop}  (source max {max_id})")
    if args.dry_run:
        print("  mode       : DRY RUN — nothing is written")

    before_l0 = _count(db_path, "l0_journal")
    before_ep = _count(db_path, "episodes")
    before_core = _count(db_path, "core_memory")
    before_layers = _layer_counts(db_path)
    print(f"\n  before: L0 {before_l0}, episodes {before_ep}, core_memory {before_core}")

    cursor = start - 1
    scanned = skipped_empty = 0
    written: dict[str, int] = {}
    failures = 0

    while cursor < stop:
        batch = source.fetch_after(cursor, cfg.batch_limit)
        if not batch.messages:
            break
        for msg in batch.messages:
            if msg.source_id > stop:
                cursor = stop
                break
            cursor = msg.source_id
            scanned += 1
            text = (msg.text or "").strip()
            if not text:
                # Same reason the daemon skips these before dispatch: an empty row
                # can never become a memory, and cataloguing them here would fill
                # the journal with rows nobody can ever distil into anything.
                skipped_empty += 1
                continue
            layer = dispatch_layer(cfg, {"role": msg.sender})
            if args.dry_run:
                written[layer] = written.get(layer, 0) + 1
                continue
            rid = await capture(
                "import",
                layer,
                cfg.user_id,
                text,
                source_msg_id=msg.source_id,
                raw_type="import",
                ts_override=msg.ts,
            )
            if rid is None:
                failures += 1
            else:
                written[layer] = written.get(layer, 0) + 1
        if scanned % 500 < cfg.batch_limit:
            print(f"    scanned {scanned}…", flush=True)

    source.close()

    if args.dry_run:
        print(f"\n  DRY RUN result: {scanned} rows scanned, {skipped_empty} empty, {sum(written.values())} would be written {written}")
        return 0

    after_l0 = _count(db_path, "l0_journal")
    after_ep = _count(db_path, "episodes")
    after_core = _count(db_path, "core_memory")
    after_layers = _layer_counts(db_path)
    inserted = after_l0 - before_l0

    print(f"\n  scanned {scanned} rows, {skipped_empty} empty, {failures} capture failures")
    print(f"  attempted {sum(written.values())} {written}; inserted {inserted} new rows")
    print(f"    ({sum(written.values()) - inserted} collapsed by L0 content dedup — the source stores some messages twice)")
    print("\n  layer movement:")
    for layer in sorted(set(before_layers) | set(after_layers)):
        was, now = before_layers.get(layer, 0), after_layers.get(layer, 0)
        print(f"    {layer:<6} {was:>6} -> {now:<6} ({now - was:+d})")

    # The invariant the whole design rests on, asserted rather than assumed: this
    # script must not distil. If episodes or core_memory moved, the import reached
    # beyond the journal and the "wake it up later" promise is already broken.
    print("\n  no-distillation check (the point of this script):")
    for name, was, now in (("episodes", before_ep, after_ep), ("core_memory", before_core, after_core)):
        verdict = "unchanged" if was == now else f"CHANGED {now - was:+d} — import reached past L0"
        print(f"    {name:<12} {was} -> {now}  {verdict}")

    broken = await verify_chain()
    print(f"\n  L0 hash chain: {len(broken)} broken link(s)")

    ok = failures == 0 and not broken and before_ep == after_ep and before_core == after_core
    print(f"\n  {'OK' if ok else 'REVIEW NEEDED'}: journal-only import complete")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Import an autohooks source into L0 without distilling.")
    ap.add_argument("--config", required=True, help="autohooks agent config (yaml)")
    ap.add_argument("--from-id", type=int, default=1, help="first source id (default 1)")
    ap.add_argument("--to-id", type=int, default=0, help="last source id (default: source max)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be written, write nothing")
    args = ap.parse_args()

    from shared.connection import connection_manager

    async def _wrapped() -> int:
        try:
            return await _run(args)
        finally:
            await connection_manager.close_all()

    return asyncio.run(_wrapped())


if __name__ == "__main__":
    raise SystemExit(main())
