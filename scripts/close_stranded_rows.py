"""Close L0 rows stranded by an early return, recording WHY each one closed.

THE PROBLEM THIS CLEANS UP. `hooks/external.py::auto_save_text` captured the row
and then returned on `verdict["bypass"]` BEFORE the watermark block, so nothing
ever wrote a status. Those rows stayed `received` — the one status `l0_tiers`
promises never to tier or archive — so they accumulated with no reader at all.
The early return is fixed; this script deals with the rows it already stranded.

WHY A SCRIPT AND NOT `replay`. `replay` distils whatever it selects, and it
bypasses the importance gate by construction. Running it over the window would
put the refused chatter straight back into memory — the opposite of the cleanup.
So rows are closed here one status at a time, and the few that deserve waking are
woken by `l0_cli.py replay --ids` instead.

THE REASONS, AND WHY THEY ARE NOT ONE LABEL. A close is only honest if a later
reader can tell deliberate closures apart. Measured on the live bases before
writing this script: of the stranded `new_message` rows, the gate refuses every
single one (score 0.20 against thresholds 0.302 and 0.240) — so
`importance_gate_bypass` is the truth for those, not a guess. The `think` and
`remember` rows never reached the gate at all: they were already written
addressably by their own entry, which is exactly what `routed_direct` means and
what `replay` does for them elsewhere. `l0_sweep`, `graph_cleanup` and
`personality_shift` are internal events that were never a message.

NOTHING IS DESTROYED, AND THAT WAS CHECKED. Closing makes a row eligible for
tiering: 30-180 days old becomes warm (`text` replaced by a preview, the full text
kept zlib-compressed in `text_z`), and past 180 days it is copied PLAINTEXT into
`l0_cold_archive` before being dropped from the journal. Every step preserves the
text, so this is reversible. `raw_type='import'` is never touched: those rows are
a deliberate "L0 only, wake later" import, and closing them would record a
(config_hash) decision that stops a window replay from ever revisiting them.

Dry run by default. `--apply` writes, after taking a backup.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Events that are a message a persona or the owner sent. Anything else in the
# journal is housekeeping, and the importance gate never saw it.
MESSAGE_EVENTS = ("new_message", "auto_save_candidate")


def classify(row: dict[str, Any]) -> tuple[str, str | None, str]:
    """Return (action, status, reason) for one stranded journal row.

    `action` is "close" or "keep". "keep" means the row is left alone: either it
    already owns a status transition elsewhere, or it is an import.
    """
    if row["raw_type"] == "import":
        return "keep", "", "import"
    try:
        decisions = json.loads(row["decisions"] or "[]")
    except (json.JSONDecodeError, TypeError):
        decisions = []
    if any(d.get("skip_distill") for d in decisions):
        # `replay` makes this same transition: the entry already wrote the row
        # addressably, so there is nothing for the distiller to do.
        return "close", "routed_direct", "skip_distill_entry"
    if not (row["text"] or "").strip():
        return "close", "gated_out", "empty_text"
    if row["event"] not in MESSAGE_EVENTS:
        return "close", "gated_out", "internal_event"
    return "close", "gated_out", "importance_gate_bypass"


def _rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT id, ts, event, layer, raw_type, user_id, text, status, decisions FROM l0_journal WHERE status='received' ORDER BY id")
    return [dict(r) for r in cur.fetchall()]


def _backup(db: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = db.with_name(f"{db.name}.pre-stranded-close-{stamp}.bak")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def _backup_sizes(db: Path) -> str:
    """Report what else is on disk, because a daemon's writes live in -wal.

    Without this the mtime of `memory.db` can look untouched while a live process
    is mid-flight; the -wal file is the honest indicator.
    """
    parts = []
    for suffix in ("", "-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        if p.exists():
            parts.append(f"{suffix or 'db'} {p.stat().st_size}B @{time.strftime('%H:%M:%S', time.localtime(p.stat().st_mtime))}")
    return "  ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--replay-ids", default=None, metavar="N,N", help="ids to LEAVE for `l0_cli.py replay --ids` instead of closing")
    ap.add_argument("--data-dir", default=None, help="override MCP_MEMORY_DATA_DIR")
    args = ap.parse_args()

    data_dir = Path(args.data_dir or os.environ.get("MCP_MEMORY_DATA_DIR", ""))
    db = data_dir / "memory.db"
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    keep_for_replay = {int(x) for x in args.replay_ids.split(",")} if args.replay_ids else set()

    print(f"base: {db}")
    print(f"files: {_backup_sizes(db)}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = _rows(conn)
    conn.close()
    print(f"stranded (status='received'): {len(rows)}")

    grouped: dict[tuple[str, str, str], list[int]] = {}
    kept: dict[str, list[int]] = {}
    for row in rows:
        action, status, reason = classify(row)
        if action == "keep" or row["id"] in keep_for_replay:
            key = reason if action == "keep" else "kept_for_replay"
            kept.setdefault(key, []).append(row["id"])
            continue
        grouped.setdefault((status, reason, row["raw_type"]), []).append(row["id"])

    print("\nCLOSE:")
    for (status, reason, raw_type), ids in sorted(grouped.items()):
        print(f"  {len(ids):5}  {status:14s} reason={reason:24s} raw_type={raw_type}")
    print("\nKEEP:")
    for reason, ids in sorted(kept.items()):
        print(f"  {len(ids):5}  {reason}")
    total = sum(len(v) for v in grouped.values())
    print(f"\nwould close {total}, keep {sum(len(v) for v in kept.values())}")

    if not args.apply:
        print("\ndry run: nothing written. Re-run with --apply to write.")
        return 0

    backup = _backup(db)
    print(f"\nbackup: {backup}")
    now = time.time()
    from features.replay import config_hash

    chash = config_hash()
    writable = sqlite3.connect(db)
    changed = 0
    try:
        for (status, reason, _raw), ids in grouped.items():
            for rid in ids:
                current = writable.execute("SELECT decisions FROM l0_journal WHERE id=?", (rid,)).fetchone()
                try:
                    decisions = json.loads((current[0] if current else None) or "[]")
                except (json.JSONDecodeError, TypeError):
                    decisions = []
                # `routed_direct` matches what `replay` writes for a skip_distill
                # row — status only, no decision entry. The others get the
                # (gate, config_hash) pair replay reads, so an unchanged config
                # leaves them closed and a changed one re-opens them.
                if status == "routed_direct":
                    writable.execute("UPDATE l0_journal SET status=?, processed_at=? WHERE id=?", (status, now, rid))
                else:
                    decisions.append({"gate": "g1", "config_hash": chash, "ts": now, "reason": reason})
                    writable.execute(
                        "UPDATE l0_journal SET status=?, processed_at=?, decisions=? WHERE id=?",
                        (status, now, json.dumps(decisions, ensure_ascii=False), rid),
                    )
                changed += 1
        writable.commit()
    finally:
        writable.close()
    print(f"closed {changed} rows")

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    left = conn.execute("SELECT status, COUNT(*) FROM l0_journal GROUP BY status ORDER BY status").fetchall()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    print(f"statuses now: {dict(left)}")
    print(f"integrity: {integrity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
