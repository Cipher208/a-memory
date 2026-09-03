#!/usr/bin/env python3
"""A1.7: docs → markdown → wiki pipeline.

Ingests documentation files (.md, .html, .txt) from a source directory into
each live agent's wiki, one page per file, wiki type guessed from path and
content (the sync_external heuristic). Idempotent: identical content is
skipped via the sync_external sha256 dedup; re-runs pick up changed files.

HTML → MD conversion: uses `markdownify` when installed (pip install
markdownify); otherwise falls back to a small stdlib regex pass covering
headings, links, emphasis, code blocks and paragraphs (loose, by design —
a rough page beats no page; refine the SSOT file if needed).

Per-agent sync runs in its own subprocess with that agent's
MCP_MEMORY_DATA_DIR (sync_skills.py pattern — env before import).

Usage:
    python scripts/docs_to_wiki.py --source ~/docs            # all agents
    python scripts/docs_to_wiki.py --source ~/docs --agent hermes
    python scripts/docs_to_wiki.py --source ~/docs --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import re
import subprocess
import sys
from pathlib import Path

ARIEL_REPO = "/home/murat/mcp-ariel-memory"
ARIEL_PY = "/home/murat/mcp-ariel-memory/.venv/bin/python3"
AGENTS = {
    "hermes": Path.home() / ".mcp-ariel-memory-hermes",
    "mimocode": Path.home() / ".mcp-ariel-memory-mimocode",
    "cowagent": Path.home() / ".mcp-ariel-memory-cowagent",
}
DOC_EXTS = {".md", ".markdown", ".html", ".htm", ".txt"}
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "_retired", "exports", "backups"}


def html_to_md(raw: str) -> str:
    """HTML → markdown. markdownify if available, else a stdlib regex pass."""
    try:
        from markdownify import markdownify as _md  # type: ignore[import-not-found]

        return _md(raw, heading_style="ATX")
    except ImportError:
        pass

    text = raw
    # drop non-content blocks
    text = re.sub(r"<(script|style|nav|footer|head)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # headings
    for level in range(6, 0, -1):
        text = re.sub(
            rf"<h{level}[^>]*>(.*?)</h{level}>",
            lambda m, lvl=level: f"\n{'#' * lvl} {m.group(1).strip()}\n",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    # links and images (both quote styles)
    text = re.sub(r'<a[^>]*href=[\"\x27]([^\"\x27]*)[\"\x27][^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<img[^>]*src=[\"\x27]([^\"\x27]*)[\"\x27][^>]*>', r"![img](\1)", text, flags=re.IGNORECASE)
    # emphasis + code (word boundary: `<b>` must not match `<body>`)
    text = re.sub(r"<(strong|b)\b[^>]*>(.*?)</\1>", r"**\2**", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<(em|i)\b[^>]*>(.*?)</\1>", r"*\2*", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<pre[^>]*>(.*?)</pre>", lambda m: f"\n```\n{m.group(1)}\n```\n", text, flags=re.DOTALL | re.IGNORECASE)
    # paragraphs and breaks
    text = re.sub(r"<(br|/p|/li|/tr|/h\d)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    # strip remaining tags, unescape entities, collapse blank lines
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def to_markdown(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in (".html", ".htm"):
        return html_to_md(text)
    return text  # .md/.markdown/.txt pass through


def collect(source: Path) -> list[Path]:
    """All convertible doc files under source (recursive, junk dirs skipped)."""
    return sorted(
        p
        for p in source.rglob("*")
        if p.is_file() and p.suffix.lower() in DOC_EXTS and not any(part in SKIP_PARTS for part in p.parts)
    )


def convert_to_ssot(source: Path, ssot_dir: Path) -> list[Path]:
    """Convert every doc into an .md page under ssot_dir (flat, stem names)."""
    pages: list[Path] = []
    for f in collect(source):
        rel = f.relative_to(source).with_suffix(".md")
        page = ssot_dir / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(to_markdown(f), encoding="utf-8")
        pages.append(page)
    return pages


def sync_one(source: Path) -> int:
    """In-process convert + sync (parent already set MCP_MEMORY_DATA_DIR)."""
    sys.path.insert(0, ARIEL_REPO)
    ssot_dir = Path(os.environ.get("DOCS_SSOT_DIR") or Path.home() / "docs-ssot" / "default")
    pages = convert_to_ssot(source, ssot_dir)
    if not pages:
        print("0 convertible files")
        return 0

    async def _run() -> dict:
        from wiki.manager import WikiManager

        wm = WikiManager(layer="user")
        # unique parent dirs only — sync_external scans a dir recursively
        dirs = sorted({str(p.parent) for p in pages})
        out = {"imported": 0, "skipped": 0, "errors": 0}
        for d in dirs:
            res = await wm.sync_external([d])
            for k in out:
                out[k] += res.get(k, 0)
        from shared.connection import connection_manager

        await connection_manager.close_all()
        return out

    res = asyncio.run(_run())
    print(f"imported={res['imported']} skipped={res['skipped']} errors={res['errors']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="A1.7: docs → markdown → wiki pipeline")
    ap.add_argument("--source", required=True, help="source directory of .md/.html/.txt docs")
    ap.add_argument("--agent", help="internal: sync one agent (parent sets env)")
    ap.add_argument("--dry-run", action="store_true", help="list convertible files, write nothing")
    args = ap.parse_args()

    source = Path(args.source).expanduser()
    if not source.is_dir():
        print(f"source dir not found: {source}", file=sys.stderr)
        return 1

    if args.dry_run:
        files = collect(source)
        print(f"{len(files)} convertible file(s):")
        for f in files:
            print(f"  {f.relative_to(source)}")
        return 0

    if args.agent:
        if args.agent not in AGENTS:
            print(f"unknown agent: {args.agent}", file=sys.stderr)
            return 1
        return sync_one(source)

    # staging SSOT (converted pages) + per-agent subprocess with env
    ssot_root = Path.home() / "docs-ssot"
    ssot_root.mkdir(parents=True, exist_ok=True)

    failed = []
    for name, data_dir in AGENTS.items():
        if not data_dir.is_dir():
            print(f"{name}: skipped (no data dir)")
            continue
        agent_ssot = ssot_root / name
        env = dict(
            os.environ,
            MCP_MEMORY_DATA_DIR=str(data_dir),
            DOCS_SSOT_DIR=str(agent_ssot),
            ARIEL_HASH_EMBEDDINGS="1",
        )
        proc = subprocess.run(
            [ARIEL_PY, __file__, "--source", str(source), "--agent", name],
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        out = (proc.stdout or "").strip().splitlines()
        err_tail = (proc.stderr or "")[-120:]
        print(f"{name}: {out[-1] if out else f'exit={proc.returncode} {err_tail}'}")
        if proc.returncode != 0:
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
