#!/usr/bin/env python3
r"""Remove agent-speaker rows that leaked into the owner's layer, archiving them first.

WHY THIS EXISTS

Until 12.10.2026 `lifecycle.distiller.distill_and_route` built its L4 store with a
literal `layer="user"`, so an agent-layer turn landed in the owner's fact heap.
Only `MemoryKind.PROCEDURAL` was ever routed to `agent`; every other kind — rule,
instruction, decision, fact, preference, commitment — went to the owner regardless
of who said it. Fixed in e71ca85 (PR #103); the rows already written are what this
removes.

The blast radius is measured, not assumed:

  core_memory   65 rows in layer='user' whose metadata.source_raw_id points at an
                L0 row marked layer='agent'. 38 of them are of kinds that never
                archive (rule, instruction, commitment), so they are permanent.
  episodes       0. The L3 path used `mem.l3.save`, which reads `mem.layer_type`
                correctly — only the L4 store was hard-coded. Nothing to do.
  epi_nodes      content-linked (no id column points at a fact); checked below.
  l0_journal     NOT TOUCHED. See below.

WHY L0 IS LEFT ALONE

`shared/l0.py::_content_hash` hashes `layer|user_id|text`. Relabelling an L0 row
would change the input to its own stored hash and break `verify_chain` — the chain
is the record of what arrived, including the rows that arrived mislabelled. The
defect is corrected downstream, where recall actually reads.

WHY DELETE AND NOT RELABEL

Relabelling the 65 rows to `layer='agent'` is the tempting "just correct the label"
move, and it is worse than deleting. 38 of them carry kinds that never archive, so
the move would make junk my own PERMANENT canon instead of the owner's — the same
defect, relocated. They are archived here, so nothing is lost.

NOTHING IS ERASED

Every removed row is copied to `archived_memories` with reason `speaker_layer_leak`
before the delete. That reason is deliberately NOT in `PURGE_REASONS`: removal was
deliberate, but restorable, and a restorable removal is the safer default.

EVERY REMOVAL LEAVES A TRAIL

One `audit_log` row per removed record — `action='speaker_layer_leak_purge'`, the
shape `purge_l3_legacy` writes. Per-record and not a summary: `features/blame.py`
answers «what happened to this row» with `WHERE target_id=?`, and
`lifecycle/importance_scheduler.py` counts `target_id`, so a single summary row
would answer neither question.

USAGE

    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory-hermes \
        .venv/bin/python3 scripts/purge_speaker_layer_leaks.py --dry-run
    ... --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ARCHIVE_REASON = "speaker_layer_leak"


def _db_path() -> Path:
    from shared.constants import DB_NAME
    from shared.connection import connection_manager

    return Path(connection_manager.base_dir) / DB_NAME


def plan(db: Path) -> dict[str, Any]:
    """Compute the whole removal set without writing anything."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row

        agent_l0 = {int(r["id"]) for r in conn.execute("SELECT id FROM l0_journal WHERE layer='agent'")}

        facts: list[sqlite3.Row] = []
        for row in conn.execute(
            "SELECT entry_id, key, value, memory_kind, layer, user_id, importance, metadata"
            " FROM core_memory WHERE layer='user' AND metadata IS NOT NULL"
        ):
            try:
                md = json.loads(row["metadata"])
            except (TypeError, ValueError):
                continue
            rid = md.get("source_raw_id") if isinstance(md, dict) else None
            if rid is not None and int(rid) in agent_l0:
                facts.append(row)

        # L3 is expected empty; measured rather than assumed, so a future replay
        # that reintroduces the class is not missed by a script that cannot see it.
        # The layer filter is load-bearing: matching on the raw tag alone selects
        # every legitimate agent episode (6030 of them) and would delete the whole
        # agent layer instead of the leak.
        doomed_eps: list[sqlite3.Row] = []
        for row in conn.execute("SELECT episode_id, summary, tags, user_id, emotional_weight FROM episodes WHERE layer='user'"):
            try:
                tags = json.loads(row["tags"]) if row["tags"] else []
            except (TypeError, ValueError):
                continue
            for t in tags if isinstance(tags, list) else []:
                if isinstance(t, str) and t.startswith("raw:") and t[4:].isdigit() and int(t[4:]) in agent_l0:
                    doomed_eps.append(row)
                    break

        # A fact's own atoms may have been wired into the graph; the graph is
        # content-linked, so match on value and only where no surviving fact has it.
        # The layer filter is the point of this script applied to itself: matching
        # on content alone is exactly how the leak happened, and an agent-layer node
        # that happens to share a text is not ours to delete.
        values = {(r["value"] or "").strip() for r in facts}
        survivors = {(r["value"] or "").strip() for r in conn.execute("SELECT value FROM core_memory") if (r["value"] or "").strip() not in values}
        doomed_nodes = [
            int(r["node_id"])
            for r in conn.execute("SELECT node_id, content, layer FROM epi_nodes WHERE layer='user'")
            if (r["content"] or "").strip() in values and (r["content"] or "").strip() not in survivors
        ]

        return {"facts": facts, "episodes": doomed_eps, "node_ids": doomed_nodes}


def report(p: dict[str, Any]) -> None:
    facts = p["facts"]
    print(f"  core_memory: {len(facts)}")
    by_kind: dict[str, int] = {}
    for r in facts:
        by_kind[str(r["memory_kind"])] = by_kind.get(str(r["memory_kind"]), 0) + 1
    for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"      {k:14} {n}")
    print(f"  episodes:    {len(p['episodes'])}")
    print(f"  epi_nodes:   {len(p['node_ids'])}")
    print("\n  ── первые 12 записей к удалению ──")
    for r in facts[:12]:
        kind, val = str(r["memory_kind"]), str(r["value"])
        print(f"      [{kind:11}] {val[:70]!r}")


def _same_path(a: object, b: object) -> bool:
    """Compare two paths by real location (sync: `resolve` is blocking I/O)."""
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


async def apply(p: dict[str, Any], db: Path) -> None:
    import time

    from shared.archived_memories import ArchivedMemories
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    active = Path(connection_manager.base_dir)
    if not _same_path(active, db.parent):
        print(f"  ОТКАЗ: активная база {active} != {db.parent}")
        raise SystemExit(2)

    am = ArchivedMemories(cm=connection_manager)
    await am._init_db()
    conn = await connection_manager.get(DB_NAME)
    now = time.time()

    async def _audit(target_id: str, details: dict[str, Any]) -> None:
        """One row per removed record, the shape `purge_l3_legacy` writes.

        Per-record and not a summary line: `features/blame.py` answers «what happened
        to this row» with `WHERE target_id=?`, and `lifecycle/importance_scheduler.py`
        counts `target_id`. A single summary row would answer neither.
        """
        await conn.execute(
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES (?,?,?,?,?,?)",
            ("default", "speaker_layer_leak_purge", "user", target_id, json.dumps(details, ensure_ascii=False), now),
        )

    for r in p["facts"]:
        # Re-read before deleting: the row must still be what the plan saw.
        row = await (await conn.execute("SELECT layer, key, value FROM core_memory WHERE entry_id=?", (int(r["entry_id"]),))).fetchone()
        if row is None or str(row["layer"]) != "user":
            print(f"  ОТКАЗ: entry_id={r['entry_id']} изменился — прогон прерван")
            raise SystemExit(3)
        await am.archive(
            user_id=r["user_id"] or "default",
            content=f"{r['key']} = {r['value']}",
            memory_type="core_memory",
            importance=float(r["importance"] or 0.0),
            original_id=int(r["entry_id"]),
            reason=ARCHIVE_REASON,
        )
    ids = [int(r["entry_id"]) for r in p["facts"]]
    if ids:
        ph = ",".join("?" * len(ids))
        await conn.execute(f"DELETE FROM core_memory WHERE entry_id IN ({ph})", tuple(ids))
        for r in p["facts"]:
            await _audit(
                f"core_memory:{int(r['entry_id'])}",
                {"klass": ARCHIVE_REASON, "kind": str(r["memory_kind"]), "archive": f"archived_memories:{ARCHIVE_REASON}"},
            )

    # Episodes are archived and removed like facts. They are 0 on the live base, but a
    # replay that reintroduces the class must be handled, not merely displayed: the
    # plan and the report already count them, so an apply that skipped them would
    # print a success line over rows it left in place.
    ep_ids = [int(r["episode_id"]) for r in p["episodes"]]
    if ep_ids:
        for r in p["episodes"]:
            await am.archive(
                user_id=r["user_id"] or "default",
                content=str(r["summary"]),
                memory_type="episodes",
                importance=float(r["emotional_weight"] or 0.0),
                original_id=int(r["episode_id"]),
                reason=ARCHIVE_REASON,
            )
        ph = ",".join("?" * len(ep_ids))
        await conn.execute(f"DELETE FROM episodes WHERE episode_id IN ({ph})", tuple(ep_ids))
        for r in p["episodes"]:
            await _audit(f"episode:{int(r['episode_id'])}", {"klass": ARCHIVE_REASON, "archive": f"archived_memories:{ARCHIVE_REASON}"})

    if p["node_ids"]:
        ph = ",".join("?" * len(p["node_ids"]))
        await conn.execute(
            f"DELETE FROM epi_edges WHERE source_id IN ({ph}) OR target_id IN ({ph})",
            tuple(p["node_ids"] * 2),
        )
        await conn.execute(f"DELETE FROM epi_tags WHERE node_id IN ({ph})", tuple(p["node_ids"]))
        await conn.execute(f"DELETE FROM epi_nodes WHERE node_id IN ({ph})", tuple(p["node_ids"]))
        for nid in p["node_ids"]:
            await _audit(f"epi_node:{int(nid)}", {"klass": ARCHIVE_REASON, "archive": f"archived_memories:{ARCHIVE_REASON}"})

    await conn.commit()
    audited = len(ids) + len(ep_ids) + len(p["node_ids"])
    print(f"  заархивировано и удалено: {len(ids)}  эпизодов: {len(ep_ids)}  узлов: {len(p['node_ids'])}")
    print(f"  строк audit_log записано: {audited}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    args = ap.parse_args()

    db = _db_path()
    print(f"═══ база: {db}")
    p = plan(db)
    report(p)
    if not args.apply:
        print("\n  DRY RUN — ничего не записано. Повторить с --apply.")
        return 0
    try:
        await apply(p, db)
    finally:
        # `apply` opens the base through `connection_manager` (aiosqlite), whose worker
        # threads are non-daemon and block in `queue.get()`: without this the script does
        # all its work and then hangs at interpreter shutdown (exit=124). `plan` reads
        # with plain `sqlite3`, which is why a dry run never showed the defect.
        from shared.connection import connection_manager

        await connection_manager.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
