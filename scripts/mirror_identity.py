#!/usr/bin/env python
"""Mirror a markdown identity file into L4 as declared canon (operator tool).

Splits on "## " headings, wraps oversized sections at paragraph boundaries
(2000-char cap = the declared-canon ceiling), writes with the SAME key scheme
as think(kind=...): canon:<kind>:<sha1[:12]>. Idempotent re-runs upsert.
Each write lands an importance_audit row with source "identity_mirror".

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

LIMIT = 2000


def split_chunks(md: str) -> list[str]:
    parts = re.split(r"(?m)^(?=## )", md)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        # bare headings carry no content; mirrors of "# Title" are noise rows
        if not p or len(p) < 30:
            continue
        if len(p) <= LIMIT:
            out.append(p)
            continue
        buf = ""
        for para in p.split("\n\n"):
            if buf and len(buf) + len(para) + 2 > LIMIT:
                out.append(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}".strip()
        if buf:
            out.append(buf)
    return out


async def mirror(paths: list[Path], kind: str, layer: str, user_id: str, importance: float, dry: bool) -> int:
    from mcp_server.context import AppContext
    from shared.connection import connection_manager

    app = AppContext()
    mem = app.mm.agent_memory(user_id) if layer == "agent" else app.mm.user_memory(user_id)
    written = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for chunk in split_chunks(text):
            key = f"canon:{kind}:{hashlib.sha1(chunk.encode('utf-8')).hexdigest()[:12]}"
            if dry:
                print(f"[dry] {path.name} {key} {len(chunk)}B")
                written += 1
                continue
            entry_id = await mem.remember(key, chunk, importance, source="identity_mirror", memory_kind=kind)
            conn = await connection_manager.get("memory.db")
            await conn.execute(
                "INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, signal_breakdown, reason, rescored_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (user_id, int(entry_id), "identity_mirror", 0.0, importance, "{}", f"mirror {path.name} kind={kind}", time.time()),
            )
            await conn.commit()
            written += 1
            print(f"mirrored {path.name} {key} {len(chunk)}B")
    await connection_manager.close_all()
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", action="append", required=True, type=Path, help="markdown file (repeatable)")
    ap.add_argument("--kind", default="rule", help="declarable MemoryKind (default: rule)")
    ap.add_argument("--layer", default="agent", choices=["agent", "user"])
    ap.add_argument("--user", default="default")
    ap.add_argument("--importance", type=float, default=0.85)
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args()
    n = asyncio.run(mirror([f for f in ns.file if f.exists()], ns.kind, ns.layer, ns.user, ns.importance, ns.dry_run))
    print(f"total: {n} chunk(s), dry_run={ns.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
