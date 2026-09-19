#!/usr/bin/env python3
"""Collapse duplicate L4 twins in core_memory (same `value`, different `key`).

Root cause: `features/session_close.py::_slug` takes the first four words of a
sentence with no stemming, so one meaning in two word-forms becomes two keys and
`UNIQUE(layer,user_id,key)` never fires. Five pairs were born 08-16.09; inflow
stopped after 16.09.

House rules: archive before delete, API-only, verify after.
  - full row -> JSONL under scripts/purge-archive/
  - row -> archived_memories via ArchivedMemories.archive()
  - delete -> CoreMemory.delete(layer,user_id,key): records core_memory_history
    and closes the temporal interval, then removes the row by key
  - audit_log entry per row

    python scripts/dedup_core_twins.py            # preview
    python scripts/dedup_core_twins.py --apply    # execute
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DATA_DIR = os.environ.get("MCP_MEMORY_DATA_DIR", str(Path.home() / ".mcp-ariel-memory-hermes"))
os.environ["MCP_MEMORY_DATA_DIR"] = DATA_DIR
os.environ.setdefault("MCP_CONFIG_PATH", str(Path(DATA_DIR) / "config.yaml"))

ARCHIVE_DIR = REPO / "scripts" / "purge-archive"
USER = "default"

DUP_SQL = """
SELECT entry_id, layer, user_id, key, value, memory_kind, importance, source,
       created_at, updated_at, visibility, expires_at
FROM core_memory
WHERE (layer, user_id, value) IN (
    SELECT layer, user_id, value FROM core_memory
    GROUP BY layer, user_id, value HAVING COUNT(*) > 1
)
ORDER BY value, importance DESC, entry_id DESC
"""


def pick(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split each duplicate group into (keep, drop): highest importance wins.

    Grouped by (layer, user_id, value) — value alone is not an identity. Two
    users can hold byte-identical text, and collapsing across them would delete
    another user's memory. Same triple the schema's UNIQUE(layer,user_id,key)
    assumes.
    """
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["layer"], r["user_id"], r["value"]), []).append(r)
    keep, drop = [], []
    for members in groups.values():
        members.sort(key=lambda r: (r["importance"] or 0.0, r["entry_id"]), reverse=True)
        keep.append(members[0])
        drop.extend(members[1:])
    return keep, drop


def _selftest() -> None:
    """Check that the grouping key keeps user_id."""
    a = {"entry_id": 1, "layer": "user", "user_id": "default", "key": "k1", "value": "v", "importance": 0.5}
    b = dict(a, entry_id=2, importance=0.9)
    keep, drop = pick([a, b])
    assert len(keep) == 1 and len(drop) == 1 and keep[0]["entry_id"] == 2, "highest importance must win"
    # same value, different user: two singletons, nothing to drop
    c = dict(a, entry_id=3, user_id="other")
    keep, drop = pick([a, c])
    assert len(keep) == 2 and not drop, "cross-user twins must never collapse"
    # same value, different layer: same rule
    d = dict(a, entry_id=4, layer="agent")
    keep, drop = pick([a, d])
    assert len(keep) == 2 and not drop, "cross-layer twins must never collapse"
    print("selftest: 3/3 ok")


async def run(apply: bool) -> int:
    from core.memory import CoreMemory
    from shared.archived_memories import ArchivedMemories
    from shared.connection import connection_manager as cm
    from shared.constants import DB_NAME

    conn = await cm.get(DB_NAME)
    cur = await conn.execute(DUP_SQL)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]

    keep, drop = pick(rows)
    before_total = (await (await conn.execute("SELECT COUNT(*) FROM core_memory")).fetchone())[0]

    print(f"base={DATA_DIR}")
    print(f"mode={'APPLY' if apply else 'dry-run'}  dup_rows={len(rows)} groups={len(keep)} drop={len(drop)} total={before_total}")
    for k, d in zip(keep, drop, strict=False):
        print(f"  keep {k['entry_id']} imp={k['importance']:.3f}  <-  drop {d['entry_id']} imp={d['importance']:.3f}  key={d['key']}")

    if not apply:
        await cm.close_all()
        return 0

    # guard: every doomed key must address exactly one row
    for d in drop:
        n = (
            await (
                await conn.execute(
                    "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=? AND key=?",
                    (d["layer"], d["user_id"], d["key"]),
                )
            ).fetchone()
        )[0]
        if n != 1:
            print(f"  ABORT: key {d['key']!r} matches {n} rows, expected 1")
            await cm.close_all()
            return 1

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = ARCHIVE_DIR / f"core-twins-{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for d in drop:
            fh.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")

    am = ArchivedMemories(cm=cm)
    await am._init_db()
    now = time.time()
    done = 0
    for d in drop:
        await am.archive(
            user_id=d["user_id"],
            content=f"{d['key']}={d['value']}",
            memory_type=d["memory_kind"] or "fact",
            importance=d["importance"],
            original_id=d["entry_id"],
            reason="duplicate_twin",
        )
        with contextlib.suppress(Exception):
            from lifecycle.transitions import record_transition

            await record_transition(
                cm, d["user_id"], "l4", f"core:{d['entry_id']}", "archived", f"archived:duplicate_twin:{path.name}", "duplicate_twin"
            )
        l4 = CoreMemory(cm=cm, layer=d["layer"])
        ok = await l4.delete(d["user_id"], d["key"], triggered_by="dedup_core_twins")
        if ok:
            done += 1
        await conn.execute(
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?,?,?,?,?,?)",
            (
                d["user_id"],
                "l4_dedup_twin",
                d["layer"],
                f"core_memory:{d['entry_id']}",
                json.dumps({"key": d["key"], "kept": d["entry_id"], "archive": path.name}, ensure_ascii=False),
                now,
            ),
        )
    await conn.commit()

    # verify
    left = await (await conn.execute(DUP_SQL)).fetchall()
    after_total = (await (await conn.execute("SELECT COUNT(*) FROM core_memory")).fetchone())[0]
    print(f"archive: {path}")
    print(f"deleted={done}/{len(drop)}  dup_left={len(left)}  total {before_total}->{after_total}")
    print("VERDICT=" + ("CLEAN" if not left and after_total == before_total - done else "LEFTOVER"))
    await cm.close_all()
    return 0 if not left and after_total == before_total - done else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: preview only)")
    ap.add_argument("--selftest", action="store_true", help="run the grouping check and exit")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return 0
    return asyncio.run(run(a.apply))


if __name__ == "__main__":
    raise SystemExit(main())
