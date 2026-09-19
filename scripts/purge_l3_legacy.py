#!/usr/bin/env python3
"""Purge legacy L3 junk (arc-echo + system-injection episodes and graph nodes).

GO: Lili, 2026-09-16 01:50 MSK.  Scope is deliberately the *production write-gate
classifiers* from `lifecycle/consolidation.py`, so the purge and the guard can
never drift apart:

    _looks_like_arc_echo(text)          -> LCM arc-snapshot pointer echoes
    _looks_like_system_injection(text)  -> cron-delivery preamble / SKILL.md body

`_looks_like_dump` (transcript head) is measured and REPORTED but never deleted:
it is broader than the GO and would also match legitimate markdown memories.

Archive-first: every deleted row is written to JSONL before deletion, and one
`audit_log` row is written per deleted row.  Edges touching a deleted
`epi_nodes` row are removed with it (no orphan edges).

Usage:
    python3 scripts/purge_l3_legacy.py             # dry-run (default)
    python3 scripts/purge_l3_legacy.py --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/murat/mcp-ariel-memory")
from lifecycle.consolidation import (
    _looks_like_arc_echo,
    _looks_like_dump,
    _looks_like_harness_limit,
    _looks_like_system_injection,
)

DB = "/home/murat/.mcp-ariel-memory-hermes/memory.db"
USER = "default"
ARCHIVE_DIR = Path("/home/murat/mcp-ariel-memory/scripts/purge-archive")


def junk_class(text: str) -> str | None:
    """Return the junk class name, or None if the row is a legitimate memory."""
    if _looks_like_arc_echo(text):
        return "arc_echo"
    if _looks_like_system_injection(text):
        return "system_injection"
    if _looks_like_harness_limit(text):
        return "harness_limit"
    return None


def classify(cur: sqlite3.Cursor, table: str, col: str) -> tuple[list[tuple], dict]:
    rows = cur.execute(f"SELECT rowid, {col} FROM {table}").fetchall()
    hit, counts, dump_only = [], {}, 0
    for rowid, text in rows:
        text = text or ""
        klass = junk_class(text)
        if klass:
            hit.append((rowid, klass, text))
            counts[klass] = counts.get(klass, 0) + 1
        elif _looks_like_dump(text):
            dump_only += 1
    counts["_total_rows"] = len(rows)
    counts["_dump_only_not_deleted"] = dump_only
    return hit, counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=15000")
    cur = conn.cursor()

    ep_hits, ep_counts = classify(cur, "episodes", "summary")
    nd_hits, nd_counts = classify(cur, "epi_nodes", "content")

    ep_ids = [r[0] for r in ep_hits]
    nd_ids = [r[0] for r in nd_hits]

    print(f"episodes : {ep_counts}")
    print(f"epi_nodes: {nd_counts}")

    if not args.apply:
        print("DRY-RUN — nothing deleted. Pass --apply to execute.")
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    now = time.time()

    # --- archive before delete ------------------------------------------------
    ep_path = ARCHIVE_DIR / f"episodes-{stamp}.jsonl"
    with ep_path.open("w", encoding="utf-8") as fh:
        for rowid, klass, text in ep_hits:
            row = cur.execute("SELECT * FROM episodes WHERE rowid=?", (rowid,)).fetchone()
            cols = [d[0] for d in cur.description]
            fh.write(json.dumps({"klass": klass, **dict(zip(cols, row, strict=True))}, ensure_ascii=False) + "\n")

    nd_path = ARCHIVE_DIR / f"epi_nodes-{stamp}.jsonl"
    with nd_path.open("w", encoding="utf-8") as fh:
        for rowid, klass, text in nd_hits:
            row = cur.execute("SELECT * FROM epi_nodes WHERE rowid=?", (rowid,)).fetchone()
            cols = [d[0] for d in cur.description]
            fh.write(json.dumps({"klass": klass, **dict(zip(cols, row, strict=True))}, ensure_ascii=False) + "\n")

    # --- edges touching deleted nodes ----------------------------------------
    edges = 0
    if nd_ids:
        marks = ",".join("?" * len(nd_ids))
        edges = cur.execute(
            f"DELETE FROM epi_edges WHERE source_id IN ({marks}) OR target_id IN ({marks})",
            nd_ids * 2,
        ).rowcount
        cur.executemany("DELETE FROM epi_nodes WHERE rowid=?", [(i,) for i in nd_ids])

    # --- episodes + audit -----------------------------------------------------
    if ep_ids:
        cur.executemany("DELETE FROM episodes WHERE rowid=?", [(i,) for i in ep_ids])
        cur.executemany(
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?,?,?,?,?,?)",
            [
                (
                    USER,
                    "l3_legacy_purge",
                    "user",
                    f"episode:{rowid}",
                    json.dumps({"klass": klass, "archive": ep_path.name}, ensure_ascii=False),
                    now,
                )
                for rowid, klass, _ in ep_hits
            ],
        )

    if nd_ids:
        cur.executemany(
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?,?,?,?,?,?)",
            [
                (
                    USER,
                    "l3_legacy_purge",
                    "user",
                    f"epi_node:{rowid}",
                    json.dumps({"klass": klass, "archive": nd_path.name}, ensure_ascii=False),
                    now,
                )
                for rowid, klass, _ in nd_hits
            ],
        )

    conn.commit()

    # --- verify ---------------------------------------------------------------
    left_ep = len([r for r in cur.execute("SELECT summary FROM episodes").fetchall() if junk_class(r[0] or "")])
    left_nd = len([r for r in cur.execute("SELECT content FROM epi_nodes").fetchall() if junk_class(r[0] or "")])
    print(f"deleted: episodes={len(ep_ids)} epi_nodes={len(nd_ids)} epi_edges={edges}")
    print(f"archive: {ep_path} | {nd_path}")
    print(f"remaining junk: episodes={left_ep} epi_nodes={left_nd}")
    print("VERDICT=" + ("CLEAN" if left_ep == 0 and left_nd == 0 else "LEFTOVER"))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
