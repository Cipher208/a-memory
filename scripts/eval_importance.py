"""Importance scorer A/B over the live L0 journal (measure, don't vibe).

Labels are proxies from write-path outcomes:
  promoted_l4  -> durable (positive),  received -> never-promoted (negative).
Reports how many negative samples the current scorer scored at/above the
0.4 gate, how many the dialogic penalty removes, and — critically — how many
POSITIVE samples the penalty would newly reject (must be 0).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.importance import evaluate_importance, structure_score

GATE = 0.4


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.path.expanduser("~/.mcp-ariel-memory-mimocode"))
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.base}/memory.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT text, status FROM l0_journal WHERE LENGTH(text) >= 20").fetchall()
    neg_total = neg_before = neg_after = pos_total = pos_before = pos_after = 0
    for r in rows:
        # v0 = dump-3 structure score; v1 = shipped scorer (structure + penalty).
        passes_before = structure_score(r["text"]) >= GATE
        passes_after = evaluate_importance(r["text"]) >= GATE
        if r["status"] == "promoted_l4":
            pos_total += 1
            pos_before += passes_before
            pos_after += passes_after
        elif r["status"] == "received":
            neg_total += 1
            neg_before += passes_before
            neg_after += passes_after
    print(f"rows={len(rows)} positive(promoted)={pos_total} negative(received)={neg_total}")
    print(f"gate>= {GATE}: positive {pos_before}->{pos_after} (must not drop)")
    print(f"gate>= {GATE}: negative {neg_before}->{neg_after} (drop = removed pollution)")


if __name__ == "__main__":
    main()
