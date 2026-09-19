#!/usr/bin/env python3
"""Collapse duplicate L3 episodes — the SAME_RAW class only.

One L0 block re-emitted the same clause set N times: every row in the group
carries the *same* single `raw:<id>` tag and is byte-identical in
(summary, tags, emotional_weight, memory_kind, layer).  407 groups / 1758 rows
were born 08-19.09; inflow stopped at the 20:49 CEST restart that deployed
`28a3b11` (find_block gate in `hooks/external.py`).  Producer is dead — the
backlog is safe to sweep.

Deliberately NOT touched:
  - DIFF_RAW  (different raw:<id> per row)  — genuinely distinct events
  - NO_RAW    (no provenance tag at all)    — a different defect, unanalysed
  - diff_gap  (continuity feature)          — read by continuity.py

House rules: archive before delete, API-only, verify after.
  - full row -> JSONL under scripts/purge-archive/
  - row -> archived_memories via ArchivedMemories.archive()
  - delete -> EpisodicMemory.delete_by_ids() (core/episodic.py), never raw SQL
  - audit_log entry per row
  - nothing links epi_nodes/epi_edges to episode_id, so the graph is untouched

    python scripts/dedup_episode_twins.py            # preview
    python scripts/dedup_episode_twins.py --apply    # execute
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import contextlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DATA_DIR = os.environ.get("MCP_MEMORY_DATA_DIR", str(Path.home() / ".mcp-ariel-memory-hermes"))
os.environ["MCP_MEMORY_DATA_DIR"] = DATA_DIR
os.environ.setdefault("MCP_CONFIG_PATH", str(Path(DATA_DIR) / "config.yaml"))

ARCHIVE_DIR = REPO / "scripts" / "purge-archive"
LAYER = "user"

SQL = """
SELECT episode_id, user_id, summary, emotional_weight, tags, created_at,
       memory_kind, layer
FROM episodes
WHERE (user_id, summary) IN (
    SELECT user_id, summary FROM episodes GROUP BY user_id, summary HAVING COUNT(*) > 1
)
ORDER BY summary, created_at, episode_id
"""


def _raws(tags: str | None) -> set[str]:
    """Every `raw:<id>` token in a tags blob. Tolerates JSON array or bare text."""
    if not tags:
        return set()
    return {t for t in tags.replace('"', " ").replace(",", " ").replace("[", " ").replace("]", " ").split() if t.startswith("raw:")}


def classify(rows: list[dict]) -> tuple[list[dict], list[dict], str]:
    """Split one duplicate group into (keep, drop, klass). keep is [] unless SAME_RAW."""
    raws = set()
    for r in rows:
        raws |= _raws(r["tags"])

    if any("diff_gap" in (r["tags"] or "") for r in rows):
        return [], [], "diff_gap"
    if not raws:
        return [], [], "NO_RAW"
    if len(raws) > 1:
        return [], [], "DIFF_RAW"

    # SAME_RAW only if every row actually carries that one tag and the rest of the
    # row is identical — otherwise the group is heterogeneous and we back off.
    if not all(_raws(r["tags"]) == raws for r in rows):
        return [], [], "SKIP_PARTIAL"
    if len({(r["user_id"], r["summary"], r["tags"], r["emotional_weight"], r["memory_kind"], r["layer"]) for r in rows}) != 1:
        return [], [], "SKIP_VARIANT"

    rows = sorted(rows, key=lambda r: (r["created_at"], r["episode_id"]))
    return [rows[0]], rows[1:], "SAME_RAW"


def _selftest() -> None:
    a = {
        "episode_id": 1,
        "user_id": "default",
        "summary": "s",
        "emotional_weight": 0.5,
        "memory_kind": None,
        "layer": "user",
        "created_at": 1.0,
        "tags": '["raw:7"]',
    }
    b = dict(a, episode_id=2, created_at=2.0)
    keep, drop, k = classify([a, b])
    assert k == "SAME_RAW" and len(keep) == 1 and len(drop) == 1 and keep[0]["episode_id"] == 1
    assert classify([a, dict(b, tags='["raw:8"]')])[2] == "DIFF_RAW"
    assert classify([dict(a, tags="[]"), dict(b, tags="[]")])[2] == "NO_RAW"
    assert classify([a, dict(b, tags='["diff_gap"]')])[2] == "diff_gap"
    assert classify([a, dict(b, emotional_weight=0.9)])[2] == "SKIP_VARIANT"
    assert classify([a, dict(b, tags="")])[2] == "SKIP_PARTIAL"
    # same text, different user: never collapse across users
    assert classify([a, dict(b, user_id="other")])[2] == "SKIP_VARIANT"
    print("selftest: 7/7 ok")


async def run(apply: bool) -> int:
    from core.episodic import EpisodicMemory
    from shared.archived_memories import ArchivedMemories
    from shared.connection import connection_manager as cm
    from shared.constants import DB_NAME

    conn = await cm.get(DB_NAME)
    cur = await conn.execute(SQL)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["user_id"], r["summary"])].append(r)

    keep: list[dict] = []
    drop: list[dict] = []
    klass = Counter()
    for members in groups.values():
        k, d, name = classify(members)
        klass[name] += 1
        keep.extend(k)
        drop.extend(d)

    before_total = (await (await conn.execute("SELECT COUNT(*) FROM episodes")).fetchone())[0]
    orphans_sql = (
        "SELECT COUNT(*) FROM epi_edges e WHERE "
        "NOT EXISTS (SELECT 1 FROM epi_nodes n WHERE n.node_id=e.source_id) OR "
        "NOT EXISTS (SELECT 1 FROM epi_nodes n WHERE n.node_id=e.target_id)"
    )
    before_orphans = (await (await conn.execute(orphans_sql)).fetchone())[0]

    print(f"base={DATA_DIR}")
    print(f"mode={'APPLY' if apply else 'dry-run'}  dup_groups={len(groups)} dup_rows={len(rows)} total={before_total}")
    print("classes: " + "  ".join(f"{k}={v}" for k, v in sorted(klass.items())))
    print(f"drop={len(drop)}  keep={len(keep)}")

    byday = Counter(time.strftime("%Y-%m-%d", time.gmtime(d["created_at"])) for d in drop)
    print("drop by day (UTC): " + "  ".join(f"{d}:{byday[d]}" for d in sorted(byday)))

    if not apply:
        for d in drop[:15]:
            print(
                f"  drop {d['episode_id']:>5} {time.strftime('%m-%d %H:%M', time.gmtime(d['created_at']))} "
                f"{_raws(d['tags']).pop():<10} {d['summary'][:52]!r}"
            )
        if len(drop) > 15:
            print(f"  ... +{len(drop) - 15} more")
        await cm.close_all()
        return 0

    if not drop:
        print("nothing to drop")
        await cm.close_all()
        return 0

    # guard: every doomed id must still match its group exactly
    doomed = {d["episode_id"] for d in drop}
    if len(doomed) != len(drop):
        print("ABORT: duplicate episode_id in drop set")
        await cm.close_all()
        return 1
    for d in drop:
        n = (
            await (
                await conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE episode_id=? AND summary=? AND tags IS ? AND layer=?",
                    (d["episode_id"], d["summary"], d["tags"], d["layer"]),
                )
            ).fetchone()
        )[0]
        if n != 1:
            print(f"  ABORT: episode {d['episode_id']} no longer matches its group (matches={n})")
            await cm.close_all()
            return 1
    for k in keep:
        if k["episode_id"] in doomed:
            print(f"  ABORT: keep id {k['episode_id']} also in drop set")
            await cm.close_all()
            return 1

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = ARCHIVE_DIR / f"episode-twins-{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for d in drop:
            fh.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")

    am = ArchivedMemories(cm=cm)
    await am._init_db()
    now = time.time()
    for d in drop:
        await am.archive(
            user_id=d["user_id"],
            content=str(d["summary"]),
            memory_type="episode",
            importance=float(d["emotional_weight"] or 0.0),
            original_id=int(d["episode_id"]),
            reason="duplicate_episode_replay",
        )
        with contextlib.suppress(Exception):
            from lifecycle.transitions import record_transition

            await record_transition(
                cm,
                d["user_id"],
                "episode",
                f"episode:{d['episode_id']}",
                "archived",
                f"archived:duplicate_episode_replay:{path.name}",
                "duplicate_episode_replay",
            )
        await conn.execute(
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?,?,?,?,?,?)",
            (
                d["user_id"],
                "l3_dedup_twin",
                d["layer"],
                f"episodes:{d['episode_id']}",
                json.dumps({"raw": sorted(_raws(d["tags"])), "archive": path.name}, ensure_ascii=False),
                now,
            ),
        )
    await conn.commit()

    # API-only delete
    ep = EpisodicMemory(cm=cm, layer=LAYER)
    deleted = await ep.delete_by_ids(sorted(doomed))

    after_total = (await (await conn.execute("SELECT COUNT(*) FROM episodes")).fetchone())[0]
    after_orphans = (await (await conn.execute(orphans_sql)).fetchone())[0]
    left = 0
    cur = await conn.execute(SQL)
    cols = [d[0] for d in cur.description]
    rest = [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]
    rg: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rest:
        rg[(r["user_id"], r["summary"])].append(r)
    for members in rg.values():
        if classify(members)[2] == "SAME_RAW":
            left += 1

    print(f"archive: {path}")
    print(f"deleted={deleted}/{len(drop)}  same_raw_left={left}  total {before_total}->{after_total}")
    print(f"orphans {before_orphans}->{after_orphans}")
    ok = deleted == len(drop) and left == 0 and after_total == before_total - deleted and after_orphans == before_orphans
    print("VERDICT=" + ("CLEAN" if ok else "LEFTOVER"))
    await cm.close_all()
    return 0 if ok else 1


async def backfill_transitions(apply: bool) -> int:
    """Reconstruct the `episode->archived` telemetry the first run dropped.

    The first --apply passed state name "l3", which is not a node in
    lifecycle/transitions.VALID_TRANSITIONS (the tier is called "episode"), so
    every record_transition call was rejected and memory_transitions stayed
    empty.  archived_memories kept original_id for all of them, so the rows are
    recoverable exactly — this is a replay of a source-backed record, not a guess.
    """
    from shared.connection import connection_manager as cm
    from shared.constants import DB_NAME

    conn = await cm.get(DB_NAME)
    files = sorted(ARCHIVE_DIR.glob("episode-twins-*.jsonl"))
    if not files:
        print("no episode-twins-*.jsonl in archive dir — cannot name the to_ref")
        await cm.close_all()
        return 1
    path = files[-1]

    rows = await (
        await conn.execute(
            "SELECT id, user_id, original_id, archived_at FROM archived_memories WHERE archive_reason='duplicate_episode_replay' ORDER BY id"
        )
    ).fetchall()
    existing = {r[0] for r in await (await conn.execute("SELECT from_ref FROM memory_transitions WHERE kind='episode->archived'")).fetchall()}

    todo = []
    for r in rows:
        ref = f"episode:{r['original_id']}"
        if ref in existing:
            continue
        todo.append((r["user_id"], ref, r["archived_at"]))

    print(f"archived rows={len(rows)}  already recorded={len(rows) - len(todo)}  to insert={len(todo)}")
    print(f"to_ref=archived:duplicate_episode_replay:{path.name}")
    if not apply:
        await cm.close_all()
        return 0

    for user_id, ref, archived_at in todo:
        ts = calendar.timegm(time.strptime(str(archived_at), "%Y-%m-%d %H:%M:%S"))
        await conn.execute(
            "INSERT INTO memory_transitions (user_id, kind, from_ref, to_ref, reason, ts) VALUES (?,?,?,?,?,?)",
            (user_id, "episode->archived", ref, f"archived:duplicate_episode_replay:{path.name}", "duplicate_episode_replay", ts),
        )
    await conn.commit()

    n = (await (await conn.execute("SELECT COUNT(*) FROM memory_transitions WHERE reason='duplicate_episode_replay'")).fetchone())[0]
    print(f"inserted={len(todo)}  now_in_table={n}")
    print("VERDICT=" + ("CLEAN" if n == len(rows) else "LEFTOVER"))
    await cm.close_all()
    return 0 if n == len(rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: preview only)")
    ap.add_argument("--selftest", action="store_true", help="run the classifier check and exit")
    ap.add_argument("--backfill-transitions", action="store_true", help="replay the episode->archived telemetry from archived_memories")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return 0
    if a.backfill_transitions:
        return asyncio.run(backfill_transitions(a.apply))
    return asyncio.run(run(a.apply))


if __name__ == "__main__":
    raise SystemExit(main())
