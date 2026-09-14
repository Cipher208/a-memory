#!/usr/bin/env python
"""Mirror a markdown identity file into L4 as declared canon (operator tool).

Splits on "## " headings, wraps oversized sections at paragraph boundaries
(2000-char cap = the declared-canon ceiling), writes with a STABLE section
key: canon:<kind>:mir:<file>:<slug>#<h6>:p<part>. Body edits supersede
through the A2.1 bi-temporal chain; sections removed between runs are deleted
by the orphan pass — the mirror REPLACES per file, it never accumulates past
versions of a persona. Each write lands an importance_audit row with source
"identity_mirror".

Usage (env selects the ariel base):
    MCP_MEMORY_DATA_DIR=~/.mcp-ariel-memory-cowagent \
    python scripts/mirror_identity.py --file ~/cow/AGENT.md --kind rule
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
import time
from pathlib import Path
from typing import Any

from shared.constants import DB_NAME

LIMIT = 2000


def split_chunks(md: str) -> list[tuple[str, int, str]]:
    """Split into (heading, part_index, text); parts respect the LIMIT cap.

    Heading (not content) defines identity: body edits supersede the same
    temporal row instead of minting a new immortal one (spec S3.1).
    """
    out: list[tuple[str, int, str]] = []
    for p in re.split(r"(?m)^(?=## )", md):
        p = p.strip()
        # bare headings carry no content; mirrors of "# Title" are noise rows
        if not p or len(p) < 30:
            continue
        heading = p.split("\n", 1)[0].strip("#").strip() or "_top"
        if len(p) <= LIMIT:
            out.append((heading, 0, p))
            continue
        buf, idx = "", 0
        for para in p.split("\n\n"):
            if buf and len(buf) + len(para) + 2 > LIMIT:
                out.append((heading, idx, buf))
                idx += 1
                buf = para
            else:
                buf = f"{buf}\n\n{para}".strip()
        if buf:
            out.append((heading, idx, buf))
    return out


def section_key(kind: str, file_stem: str, heading: str, part: int) -> str:
    """canon:<kind>:mir:<file>:<slug>#<h6>:p<part> — content-independent key.

    slug keeps unicode word chars (Cyrillic = data, policy-legal); h6 of the
    full heading guards against 40-char truncation collisions.
    """
    slug = re.sub(r"\W+", "_", heading, flags=re.UNICODE).strip("_")[:40] or "_sec"
    h6 = hashlib.sha1(heading.encode("utf-8")).hexdigest()[:6]
    return f"canon:{kind}:mir:{file_stem}:{slug}#{h6}:p{part}"


async def mirror(
    paths: list[Path],
    kind: str,
    layer: str,
    user_id: str,
    importance: float,
    dry: bool,
    mem: Any,
    cm: Any,
    ns: str | None = None,
) -> int:
    """Per-file REPLACE: upsert all chunks, then delete keys this run did not write.

    ``ns`` overrides the key namespace (default: the file stem). Two different
    files sharing a stem (e.g. ~/AGENTS.md and .config/opencode/AGENTS.md) MUST
    mirror with distinct ``ns`` or each run orphans the other's rows.
    """
    conn = await cm.get(DB_NAME)
    written = 0
    for path in paths:
        stem = ns or path.stem
        text = path.read_text(encoding="utf-8")
        written_keys: set[str] = set()
        for heading, part, chunk in split_chunks(text):
            key = section_key(kind, stem, heading, part)
            written_keys.add(key)
            if dry:
                print(f"[dry] {path.name} {key} {len(chunk)}B")
                written += 1
                continue
            entry_id = await mem.remember(key, chunk, importance, source="identity_mirror", memory_kind=kind)
            await conn.execute(
                "INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, signal_breakdown, reason, rescored_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (user_id, int(entry_id), "identity_mirror", 0.0, importance, "{}", f"mirror {path.name} kind={kind}", time.time()),
            )
            await conn.commit()
            written += 1
            print(f"mirrored {path.name} {key} {len(chunk)}B")
        # orphan pass: same file+kind, keys absent from this run (section
        # deleted / repartitioned / renamed heading)
        cur = await conn.execute(
            "SELECT key FROM core_memory WHERE layer=? AND user_id=? AND source='identity_mirror' AND key LIKE ?",
            (layer, user_id, f"canon:{kind}:mir:{stem}:%"),
        )
        for row in await cur.fetchall():
            old_key = str(row["key"])  # sqlite3.Row via connection_manager row_factory
            if old_key in written_keys:
                continue
            if dry:
                print(f"[dry-orphan] {old_key}")
                continue
            if await mem.l4.delete(user_id, old_key, triggered_by="identity_mirror"):
                print(f"orphaned {old_key}")
    return written


_LEGACY_SHA_KEY_RE = re.compile(r"^canon:[a-z_]+:[0-9a-f]{12}$")


async def check_drift(paths: list[Path], kind: str, layer: str, user_id: str, cm: Any, ns: str | None = None) -> dict[str, list[str]]:
    """Staleness guard: identity files vs their L4 mirror rows — read-only.

    stale   — key is planned and the stored body differs (edited after last mirror)
    missing — key is planned but no row exists (section never mirrored)
    extra   — stored row for this file+kind is not in the plan (deleted/renamed section)
    Re-mirroring the file resolves all three; this writes nothing.
    ``ns`` must match the namespace the file was mirrored under (see mirror()).
    """
    conn = await cm.get(DB_NAME)
    planned: dict[str, str] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for heading, part, chunk in split_chunks(text):
            planned[section_key(kind, ns or path.stem, heading, part)] = chunk
    stored: dict[str, str] = {}
    for path in paths:
        cur = await conn.execute(
            "SELECT key, value FROM core_memory WHERE layer=? AND user_id=? AND source='identity_mirror' AND key LIKE ?",
            (layer, user_id, f"canon:{kind}:mir:{ns or path.stem}:%"),
        )
        for row in await cur.fetchall():
            stored[str(row["key"])] = str(row["value"])
    stale = sorted(k for k, chunk in planned.items() if k in stored and stored[k] != chunk)
    missing = sorted(k for k in planned if k not in stored)
    extra = sorted(k for k in stored if k not in planned)
    return {"stale": stale, "missing": missing, "extra": extra}


async def migrate_sha_keys(mem: Any, cm: Any, user_id: str, layer: str, dry: bool = False) -> tuple[int, list[str]]:
    """One-time: retire pre-2026-09-14 sha-keyed mirror rows.

    A legacy row is deleted only when importance_audit maps it to a mirrored
    file (reason='mirror <name> kind=<k>'); unmapped rows are reported and
    left for operator judgment — no silent guesses. dry=True previews the
    same report without deleting anything (guard for live bases). Re-run
    mirror afterwards to re-write the same files under stable keys.
    """
    conn = await cm.get(DB_NAME)
    cur = await conn.execute(
        "SELECT entry_id, key FROM core_memory WHERE layer=? AND user_id=? AND source='identity_mirror'",
        (layer, user_id),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    removed, kept = 0, []
    for row in rows:
        key = str(row["key"])
        if not _LEGACY_SHA_KEY_RE.match(key):
            continue
        audit = await (
            await conn.execute(
                "SELECT reason FROM importance_audit WHERE chunk_id=? AND source='identity_mirror' LIMIT 1",
                (int(row["entry_id"]),),
            )
        ).fetchone()
        if audit:
            if dry:
                removed += 1
                print(f"[dry-migrate] would retire {key} ({audit[0]})")
            elif await mem.l4.delete(user_id, key, triggered_by="identity_mirror_migration"):
                removed += 1
                print(f"migrated out legacy {key} ({audit[0]})")
        else:
            kept.append(key)
            print(f"unmapped legacy {key}: kept (no audit row — operator decision)")
    return removed, kept


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", action="append", required=True, type=Path, help="markdown file (repeatable)")
    ap.add_argument("--kind", default="rule", help="declarable MemoryKind (default: rule)")
    ap.add_argument("--layer", default="agent", choices=["agent", "user"])
    ap.add_argument("--user", default="default")
    ap.add_argument("--importance", type=float, default=0.85)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="report drift vs the last mirror (read-only); exit 1 on drift")
    ap.add_argument("--ns", default=None, help="key namespace (default: file stem); required to disambiguate different files sharing a stem")
    ap.add_argument("--migrate-sha-keys", action="store_true", help="retire audited sha-keyed legacy mirror rows first")
    ns = ap.parse_args()
    files = [f for f in ns.file if f.exists()]

    from mcp_server.context import AppContext
    from shared.connection import connection_manager

    async def _run() -> int:
        app = AppContext()
        mem = app.mm.agent_memory(ns.user) if ns.layer == "agent" else app.mm.user_memory(ns.user)
        if ns.check:
            drift = await check_drift(files, ns.kind, ns.layer, ns.user, connection_manager, ns=ns.ns)
            for class_, keys in drift.items():
                for key in keys:
                    print(f"{class_}: {key}")
            total = sum(len(v) for v in drift.values())
            print(f"check: {'clean' if not total else f'{total} drift item(s)'}")
            await connection_manager.close_all()
            return total
        if ns.migrate_sha_keys:
            removed, unmapped = await migrate_sha_keys(mem, connection_manager, ns.user, ns.layer, ns.dry_run)
            print(f"migrate{' (dry)' if ns.dry_run else ''}: {removed} legacy row(s), {len(unmapped)} unmapped kept")
        total = await mirror(files, ns.kind, ns.layer, ns.user, ns.importance, ns.dry_run, mem, connection_manager, ns=ns.ns)
        await connection_manager.close_all()
        return total

    n = asyncio.run(_run())
    if ns.check:
        return 1 if n else 0
    print(f"total: {n} chunk(s), dry_run={ns.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
