#!/usr/bin/env python3
r"""Remove another agent's distilled memories from a base, archiving them first.

WHY THIS EXISTS

An autohooks source can be multi-agent (cowagent's `index.db` carries `agent_id`
in {'', celeste, ilian, ...}) and a config that filters on `role` alone will tail
every agent's chat into one persona's memory. `cowagent.yaml` now filters on the
agent, and the rows already captured are marked `status='foreign_agent'` in
`l0_journal`. Marking fixes the *raw* journal, but by then some of those rows had
already been distilled: they are in `episodes`, in `core_memory`, and in the
`epi_nodes`/`epi_edges` graph. Marking L0 does not remove them, because L0 is not
what recall reads.

WHY IT IS NOT A RANGE DELETE, AND WHY THAT IS THE WHOLE POINT

`episodes.episode_id` and `epi_nodes.node_id` are independent sequences that
**overlap in value**: in the base this was written for, 1402 of 1580 episode ids
were also live node ids. `epi_edges.source_id`/`target_id` reference NODES. So an
id-range or naive id-join delete removes thousands of graph edges belonging to
entirely different memories — 8359 "linked rows" in the first draft of this
script were an artefact of that collision, and none of them were foreign.

Everything here therefore selects by PROVENANCE, never by id arithmetic:

  episodes      tag `raw:<l0_id>` where that L0 row is foreign
  core_memory   `metadata.source_raw_id` is a foreign L0 id
  epi_nodes     `content` equals a foreign episode's summary (the graph is
                content-linked; there is no id column pointing at an episode)

A node is only removed when its content does **not** also match a surviving
episode, so a text that legitimately belongs to the persona is never taken out
because a foreign row happened to produce the same summary.

NOTHING IS ERASED

Every removed row is copied to `archived_memories` with its reason before the
delete, using the same class `features.compression` uses. `--restore-archived`
puts a run back from those rows. Deleting without archiving would be the same
class of mistake as the bug that put the rows there.

The L0 hash chain is not touched at all: `l0_journal` rows are left in place with
`status='foreign_agent'`, so `verify_chain` stays clean and the chain remains a
complete record of what arrived.

USAGE

    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory-cowagent \\
        .venv/bin/python3 scripts/purge_foreign_rows.py --dry-run
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory-cowagent \\
        .venv/bin/python3 scripts/purge_foreign_rows.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ARCHIVE_REASON = "foreign_agent_contamination"


def _db_path() -> Path:
    import os

    base = os.environ.get("MCP_MEMORY_DATA_DIR") or str(Path.home() / ".mcp-ariel-memory")
    return Path(base) / "memory.db"


def _marked_l0_ids(db: Path) -> set[int]:
    """L0 row ids already marked as another agent's (the provenance root)."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return {int(r[0]) for r in conn.execute("SELECT id FROM l0_journal WHERE status='foreign_agent'")}


def _raw_tags(tags: Any) -> list[int]:
    try:
        parsed = json.loads(tags) if tags else []
    except (TypeError, ValueError):
        return []
    out: list[int] = []
    for t in parsed if isinstance(parsed, list) else []:
        if isinstance(t, str) and t.startswith("raw:") and t[4:].isdigit():
            out.append(int(t[4:]))
    return out


def plan(db: Path) -> dict[str, Any]:
    """Compute the whole removal set without writing anything."""
    marked = _marked_l0_ids(db)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row

        # --- episodes: via the raw:<l0_id> provenance tag ---
        foreign_eps: list[sqlite3.Row] = []
        keep_summaries: set[str] = set()
        for row in conn.execute("SELECT episode_id, summary, layer, user_id, emotional_weight, tags FROM episodes"):
            raws = _raw_tags(row["tags"])
            if raws and any(r in marked for r in raws):
                foreign_eps.append(row)
            else:
                keep_summaries.add((row["summary"] or "").strip())
        foreign_summaries = {(r["summary"] or "").strip() for r in foreign_eps}

        # --- nodes: content-linked, and only when no surviving episode has it ---
        # `keep_summaries` excludes anything a legitimate episode also produced.
        doomed_nodes = [
            r
            for r in conn.execute("SELECT node_id, content, node_type, layer, confidence FROM epi_nodes")
            if (r["content"] or "").strip() in foreign_summaries and (r["content"] or "").strip() not in keep_summaries
        ]
        node_ids = [int(r["node_id"]) for r in doomed_nodes]

        # --- facts: via metadata.source_raw_id ---
        foreign_facts: list[sqlite3.Row] = []
        for row in conn.execute("SELECT entry_id, key, value, layer, user_id, importance, metadata FROM core_memory"):
            meta = row["metadata"]
            if not meta:
                continue
            try:
                md = json.loads(meta)
            except (TypeError, ValueError):
                continue
            if isinstance(md, dict) and md.get("source_raw_id") in marked:
                foreign_facts.append(row)

        edges = tags = 0
        if node_ids:
            ph = ",".join("?" * len(node_ids))
            edges = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM epi_edges WHERE source_id IN ({ph}) OR target_id IN ({ph})",
                    node_ids + node_ids,
                ).fetchone()[0]
            )
            tags = int(conn.execute(f"SELECT COUNT(*) FROM epi_tags WHERE node_id IN ({ph})", node_ids).fetchone()[0])

    return {
        "marked_l0": len(marked),
        "episodes": foreign_eps,
        "nodes": doomed_nodes,
        "node_ids": node_ids,
        "facts": foreign_facts,
        "edges": edges,
        "tags": tags,
    }


def report(p: dict[str, Any], db: Path) -> None:
    print(f"  база: {db}")
    print(f"  помеченных чужих строк L0 (источник происхождения): {p['marked_l0']}")
    print()
    print(f"  эпизодов к удалению:   {len(p['episodes'])}")
    print(f"  узлов графа:           {len(p['node_ids'])}")
    print(f"  рёбер графа:           {p['edges']}")
    print(f"  тегов узлов:           {p['tags']}")
    print(f"  фактов core_memory:    {len(p['facts'])}")
    print()
    if p["facts"]:
        print("  факты:")
        for r in p["facts"]:
            print(f"    entry_id={r['entry_id']:<6} layer={r['layer']:<6} {str(r['key'])[:46]!r}")
    if p["episodes"]:
        print()
        print("  примеры эпизодов:")
        for r in list(p["episodes"])[:8]:
            print(f"    ep={r['episode_id']:<6} {str(r['summary'])[:66]!r}")


def _same_path(a: object, b: object) -> bool:
    """Compare two paths by real location (sync: `resolve` is blocking I/O)."""
    import os

    return os.path.realpath(str(a)) == os.path.realpath(str(b))


async def apply(p: dict[str, Any], db: Path) -> None:
    from shared.archived_memories import ArchivedMemories
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    if not _same_path(connection_manager.base_dir, db.parent):
        print(f"  ОТКАЗ: активная база {connection_manager.base_dir} != {db.parent}")
        raise SystemExit(2)

    am = ArchivedMemories(cm=connection_manager)
    await am._init_db()
    conn = await connection_manager.get(DB_NAME)

    archived = 0
    for r in p["episodes"]:
        await am.archive(
            user_id=r["user_id"] or "default",
            content=r["summary"] or "",
            memory_type="episode",
            importance=float(r["emotional_weight"] or 0.0),
            original_id=int(r["episode_id"]),
            reason=ARCHIVE_REASON,
        )
        archived += 1
    for r in p["facts"]:
        await am.archive(
            user_id=r["user_id"] or "default",
            content=f"{r['key']} = {r['value']}",
            memory_type="core_memory",
            importance=float(r["importance"] or 0.0),
            original_id=int(r["entry_id"]),
            reason=ARCHIVE_REASON,
        )
        archived += 1
    print(f"  заархивировано: {archived}")

    node_ids = p["node_ids"]
    if node_ids:
        ph = ",".join("?" * len(node_ids))
        await conn.execute(f"DELETE FROM epi_edges WHERE source_id IN ({ph}) OR target_id IN ({ph})", node_ids + node_ids)
        await conn.execute(f"DELETE FROM epi_tags WHERE node_id IN ({ph})", node_ids)
        await conn.execute(f"DELETE FROM epi_nodes WHERE node_id IN ({ph})", node_ids)

    ep_ids = [int(r["episode_id"]) for r in p["episodes"]]
    if ep_ids:
        ph = ",".join("?" * len(ep_ids))
        await conn.execute(f"DELETE FROM episodes WHERE episode_id IN ({ph})", ep_ids)

    fact_ids = [int(r["entry_id"]) for r in p["facts"]]
    if fact_ids:
        ph = ",".join("?" * len(fact_ids))
        await conn.execute(f"DELETE FROM core_memory WHERE entry_id IN ({ph})", fact_ids)

    await conn.commit()


def _counts(db: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return {
            t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("episodes", "core_memory", "epi_nodes", "epi_edges", "l0_journal")
        }


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="really delete (default: report only)")
    ap.add_argument("--dry-run", action="store_true", help="report only (the default)")
    args = ap.parse_args()

    db = _db_path()
    if not db.exists():
        print(f"  нет базы: {db}")
        return 2

    print("  purge_foreign_rows — отбор по происхождению, не по диапазонам id")
    p = plan(db)
    report(p, db)

    if not args.apply:
        print()
        print("  РЕЖИМ ОТЧЁТА — ничего не записано. Для удаления: --apply")
        return 0

    before = _counts(db)
    print()
    await apply(p, db)
    after = _counts(db)
    print()
    print("  изменение:")
    for k in before:
        d = after[k] - before[k]
        print(f"    {k:<12} {before[k]:>6} -> {after[k]:<6} ({d:+d})")

    from shared.connection import connection_manager
    from shared.l0 import verify_chain

    broken = await verify_chain()
    print(f"  L0 цепочка нарушений: {len(broken)}")
    await connection_manager.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
