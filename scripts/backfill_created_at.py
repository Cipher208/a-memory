#!/usr/bin/env python3
r"""Re-date memories that were written by a backfill instead of by their source.

WHY THIS IS NEEDED

`created_at` is supposed to answer "when did this happen". Recall asks it, the
30-day sweeper asks it, and a persona's sense of her own timeline depends on it.
Three writers used to hard-code the clock instead:

  * `shared/l0.py` — `ts = ts_override or time.time()`, and no caller in the
    hook path passed `ts_override` (the daemon HAD it: `"ts": msg.ts`);
  * `core/episodic.py` — `EpisodicMemory.save` inserted `time.time()`;
  * `core/memory.py` — `CoreMemory.save` used one `now` for both `created_at`
    and `updated_at`.

So every path that distils OLD text stamped the hour it ran onto material from
weeks earlier. Measured on a live base: 1937 agent-layer episodes whose source
messages span 15.09..04.10 all read 04.10 09:15..13:42 — the two hours of the
replay. The user layer kept its dates only because its history came through
`features/import_export.py`, which passes `created_at` explicitly; the asymmetry
was the bug, not a property of the layers.

The code is fixed. This script repairs what the old code already wrote.

HOW A TRUE TIME IS RECOVERED

Nothing is guessed. Each row carries its own provenance back to the source:

  episodes.tags          contains `raw:<l0_id>`
  core_memory.metadata   contains `source_raw_id: <l0_id>`
  core_memory (promoted) is linked by `memory_transitions`
                         (`episode:<id>` -> `core:<id>`)
  memory_transitions.ts  gives the promotion time, not the episode's

  l0_journal.id = <l0_id>  ->  l0_journal.source_msg_id
                           ->  <source>.messages.id
                           ->  <source>.messages.timestamp      <- THE TIME

A row whose chain does not resolve is REPORTED AND LEFT ALONE. It is never given
an invented date, because a wrong date is worse than a missing one: it makes
recall lie confidently.

NOTE ON `updated_at`, DELIBERATELY UNTOUCHED. It says when the row was last
written, which really was the backfill. Rewriting it would destroy the only
evidence of when the repair ran.

READ-ONLY BY DEFAULT. `--apply` is required to write, and it takes a backup of
the database first.

RUN THIS AFTER A CATCH-UP, NOT ONLY ONCE. This script resolves the time from the
SOURCE (`l0_journal.source_msg_id` -> the chat database), which is strictly better
than the journal's own `ts`. For 539 legacy rows that `ts` is the IMPORT time, not
the message time: they were captured by the old daemon, which sent the timestamp in
its payload while the hook ignored it. A `replay` can therefore only date those rows
from `l0.ts` (`features/replay.py` passes `row["ts"]`), so the episodes it creates
land at import time. Re-running this script afterwards moves them to the true time,
because provenance does not care how the row got there.

The order is: retime the base -> catch up with a wide replay window -> retime again.
The second dry run is also the proof that the first one worked: it says how many rows
would still move.

USAGE

    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory-hermes \\
        .venv/bin/python3 scripts/backfill_created_at.py --source ~/.hermes/state.db
    # ... inspect the report, then:
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory-hermes \\
        .venv/bin/python3 scripts/backfill_created_at.py --source ~/.hermes/state.db --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

_RAW_TAG = re.compile(r"raw:(\d+)")


def _local(ts: float) -> dt.datetime:
    """Localtime as a tz-aware datetime.

    tz-aware on purpose: the operator reads these dates against her own clock,
    and a naive datetime is a guess about whose clock it is.
    """
    return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).astimezone()


def _human(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _local(ts).strftime("%d.%m.%Y %H:%M")


def _day(ts: float | None) -> str:
    if ts is None:
        return "?"
    return _local(ts).strftime("%d.%m")


def _l0_times(mem: sqlite3.Connection) -> dict[int, tuple[float | None, int | None]]:
    """l0 id -> (its own ts, its source_msg_id)."""
    return {
        int(r[0]): (float(r[1]) if r[1] is not None else None, int(r[2]) if r[2] is not None else None)
        for r in mem.execute("SELECT id, ts, source_msg_id FROM l0_journal")
    }


def _source_times(src: sqlite3.Connection, ids: set[int]) -> dict[int, float]:
    """Source message id -> timestamp, in batches (the fast half of the join)."""
    out: dict[int, float] = {}
    batch = sorted(ids)
    for i in range(0, len(batch), 900):
        chunk = batch[i : i + 900]
        q = ",".join("?" * len(chunk))
        for rid, ts in src.execute(f"SELECT id, timestamp FROM messages WHERE id IN ({q})", chunk):
            if ts is not None:
                out[int(rid)] = float(ts)
    return out


def _resolve(l0: dict[int, tuple[float | None, int | None]], src_ts: dict[int, float], l0_id: int) -> tuple[float | None, str]:
    """Resolve the true time for one journal row, and say how it was obtained."""
    entry = l0.get(l0_id)
    if entry is None:
        return None, "no l0 row"
    _own_ts, smid = entry
    if smid is None:
        return None, "l0 row has no source_msg_id"
    ts = src_ts.get(smid)
    if ts is None:
        return None, f"source message {smid} not found"
    return ts, "source"


def planet_report(mem: sqlite3.Connection, src: sqlite3.Connection) -> dict:
    """Work out every re-dating that is justified, without writing anything."""
    l0 = _l0_times(mem)

    # --- episodes ---
    ep_rows = list(mem.execute("SELECT episode_id, layer, created_at, tags FROM episodes"))
    ep_want: dict[int, float] = {}
    ep_reasons: Counter[str] = Counter()

    # Group episodes by the journal row they name, then resolve each journal row
    # ONCE and share the answer: 1937 agent episodes hang off only 210 distinct
    # raw ids, so resolving per episode would repeat the same lookup thousands of
    # times and hide the fact that one resolution served many rows.
    ep_by_l0: dict[int, list[int]] = {}
    for eid, _layer, _created, tags in ep_rows:
        m = _RAW_TAG.search(tags or "")
        if not m:
            ep_reasons["no raw:<id> tag — cannot resolve"] += 1
            continue
        ep_by_l0.setdefault(int(m.group(1)), []).append(int(eid))

    needed: set[int] = set()
    for l0_id in ep_by_l0:
        entry = l0.get(l0_id)
        if entry and entry[1] is not None:
            needed.add(entry[1])
    src_ts = _source_times(src, needed)

    for l0_id, eids in ep_by_l0.items():
        ts, why = _resolve(l0, src_ts, l0_id)
        if ts is None:
            ep_reasons[why] += len(eids)
            continue
        for eid in eids:
            ep_want[eid] = ts
        ep_reasons["resolved from the source message"] += len(eids)

    # --- core_memory: two provenances ---
    cm_rows = list(mem.execute("SELECT entry_id, layer, created_at, metadata, source FROM core_memory"))
    cm_l0: dict[int, list[int]] = {}
    cm_reasons: Counter[str] = Counter()
    for entry_id, _layer, _created, metadata, _source in cm_rows:
        rid = None
        if metadata:
            try:
                rid = json.loads(metadata).get("source_raw_id")
            except (ValueError, TypeError):
                rid = None
        if rid is not None:
            cm_l0.setdefault(int(rid), []).append(int(entry_id))

    # promoted rows: memory_transitions episode:<id> -> core:<id>
    promo: dict[int, int] = {}
    if _table_exists(mem, "memory_transitions"):
        for from_ref, to_ref in mem.execute("SELECT from_ref, to_ref FROM memory_transitions WHERE kind='episode->l4'"):
            try:
                ep_id = int(str(from_ref).split(":")[1])
                cm_id = int(str(to_ref).split(":")[1])
            except (IndexError, ValueError):
                continue
            promo[cm_id] = ep_id
    ep_meta = {int(r[0]): (r[1], r[2]) for r in mem.execute("SELECT episode_id, tags, created_at FROM episodes")}

    needed2: set[int] = set()
    for rid in cm_l0:
        entry = l0.get(rid)
        if entry and entry[1] is not None:
            needed2.add(entry[1])
    for cm_id, ep_id in promo.items():
        tags = ep_meta.get(ep_id, (None, None))[0]
        m = _RAW_TAG.search(tags or "")
        if m:
            entry = l0.get(int(m.group(1)))
            if entry and entry[1] is not None:
                needed2.add(entry[1])
    src_ts.update(_source_times(src, needed2 - set(src_ts)))

    cm_want: dict[int, float] = {}
    for rid, cm_ids in cm_l0.items():
        ts, why = _resolve(l0, src_ts, rid)
        if ts is None:
            cm_reasons[f"direct: {why}"] += len(cm_ids)
            continue
        for cm_id in cm_ids:
            cm_want[cm_id] = ts
        cm_reasons["resolved directly (metadata.source_raw_id)"] += len(cm_ids)

    for cm_id, ep_id in promo.items():
        if cm_id in cm_want:
            continue
        tags = ep_meta.get(ep_id, (None, None))[0]
        m = _RAW_TAG.search(tags or "")
        if not m:
            cm_reasons["promoted: episode has no raw:<id> tag"] += 1
            continue
        ts, why = _resolve(l0, src_ts, int(m.group(1)))
        if ts is None:
            cm_reasons[f"promoted: {why}"] += 1
            continue
        cm_want[cm_id] = ts
        cm_reasons["resolved via episode promotion"] += 1

    return {
        "episodes": {"rows": ep_rows, "want": ep_want, "reasons": ep_reasons},
        "core_memory": {"rows": cm_rows, "want": cm_want, "reasons": cm_reasons},
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _spread(rows: list[tuple[float]], index: int = 0) -> dict[str, int]:
    c: Counter[str] = Counter(_day(r[index]) for r in rows if r[index] is not None)
    return dict(sorted(c.items(), key=lambda kv: (kv[0][3:], kv[0][:2])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.environ.get("MCP_MEMORY_DATA_DIR"), help="a-memory data dir (default: $MCP_MEMORY_DATA_DIR)")
    ap.add_argument("--source", required=True, help="chat database holding the original timestamps (e.g. ~/.hermes/state.db)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    ap.add_argument("--only-changed", action="store_true", default=True, help="skip rows already at their source time")
    args = ap.parse_args()

    if not args.data_dir:
        print("need --data-dir or MCP_MEMORY_DATA_DIR", file=sys.stderr)
        return 2
    data_dir = Path(os.path.expanduser(args.data_dir))
    db_path = data_dir / "memory.db"
    src_path = Path(os.path.expanduser(args.source))
    for p in (db_path, src_path):
        if not p.exists():
            print(f"not found: {p}", file=sys.stderr)
            return 2

    print(f"  база памяти : {db_path}")
    print(f"  источник    : {src_path}")
    print(f"  режим       : {'ЗАПИСЬ' if args.apply else 'только отчёт (--apply чтобы записать)'}")
    print()

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    mem = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if not args.apply else sqlite3.connect(str(db_path))
    try:
        plan = planet_report(mem, src)

        # ---------------- episodes ----------------
        ep = plan["episodes"]
        print("=== ЭПИЗОДЫ ===")
        print(f"  всего: {len(ep['rows'])}, датировка восстановима: {len(ep['want'])}")
        for why, n in ep["reasons"].most_common():
            print(f"    {why}: {n}")
        before = _spread(ep["rows"], index=2)
        after_pairs = [(ep["want"].get(int(eid), created),) for eid, _l, created, _t in ep["rows"]]
        after = _spread(after_pairs, index=0)
        print(f"  БЫЛО  по дням: {before}")
        print(f"  СТАНЕТ по дням: {after}")
        shifts = [
            (eid, created, ep["want"][int(eid)])
            for eid, _l, created, _t in ep["rows"]
            if int(eid) in ep["want"] and abs(ep["want"][int(eid)] - created) > 2.0
        ]
        print(f"  строк с реальным сдвигом: {len(shifts)}")
        for eid, old, new in shifts[:5]:
            print(f"    episode {eid}: {_human(old)} -> {_human(new)}")
        print()

        # ---------------- core_memory ----------------
        cm = plan["core_memory"]
        print("=== CORE_MEMORY (L4) ===")
        print(f"  всего: {len(cm['rows'])}, датировка восстановима: {len(cm['want'])}")
        for why, n in cm["reasons"].most_common():
            print(f"    {why}: {n}")
        print(f"  БЫЛО  по дням: {_spread(cm['rows'], index=2)}")
        after_cm = [(cm["want"].get(int(eid), created),) for eid, _l, created, _m, _s in cm["rows"]]
        print(f"  СТАНЕТ по дням: {_spread(after_cm, index=0)}")
        cm_shifts = [
            (eid, created, cm["want"][int(eid)])
            for eid, _l, created, _m, _s in cm["rows"]
            if int(eid) in cm["want"] and abs(cm["want"][int(eid)] - created) > 2.0
        ]
        print(f"  строк с реальным сдвигом: {len(cm_shifts)}")
        for eid, old, new in cm_shifts[:5]:
            print(f"    core {eid}: {_human(old)} -> {_human(new)}")
        print()

        if not args.apply:
            print("  НИЧЕГО НЕ ЗАПИСАНО (--apply чтобы применить)")
            return 0

        backup = db_path.with_name(f"{db_path.name}.pre-redate-{time.strftime('%Y%m%dT%H%M%S')}.bak")
        shutil.copy2(db_path, backup)
        print(f"  бэкап: {backup}")

        now = time.time()
        written_ep = written_cm = 0
        for eid, _l, _created, _t in ep["rows"]:
            ts = ep["want"].get(int(eid))
            if ts is None:
                continue
            mem.execute("UPDATE episodes SET created_at=? WHERE episode_id=?", (ts, int(eid)))
            written_ep += 1
        for eid, _l, _created, _m, _s in cm["rows"]:
            ts = cm["want"].get(int(eid))
            if ts is None:
                continue
            mem.execute("UPDATE core_memory SET created_at=? WHERE entry_id=?", (ts, int(eid)))
            written_cm += 1
        mem.commit()
        print(f"  записано: эпизодов {written_ep}, L4 {written_cm}  (за {time.time() - now:.1f}s)")
        print("  updated_at НЕ тронут намеренно: он говорит, когда строку писали, а это правда.")
        return 0
    finally:
        src.close()
        mem.close()


if __name__ == "__main__":
    raise SystemExit(main())
