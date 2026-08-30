#!/usr/bin/env python3
"""D2.3: sync the shared skill SSOT into each agent's wiki (Skill=Memory).

SSOT layout:  ~/skills-ssot/skill/*.md  (git repo — the shared skill set)
Target:       each live agent's ariel wiki (hermes / mimocode / cowagent).

Uses WikiManager.sync_external (copy + sha256 dedup + index), NOT symlinks:
per-agent SQLite indexes stay consistent and cross-agent writes land in the
SSOT only via git (the SSOT is the write surface; agents read copies).

Run manually or from cron. Each agent syncs in its own subprocess so
connection_manager picks up that instance's MCP_MEMORY_DATA_DIR.

Usage:
    python scripts/sync_skills.py            # sync all live agents
    python scripts/sync_skills.py --bootstrap  # create the SSOT skeleton
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import os
import subprocess
import sys
from pathlib import Path

SSOT_DIR = Path.home() / "skills-ssot"
ARIEL_REPO = "/home/murat/mcp-ariel-memory"
ARIEL_PY = "/home/murat/mcp-ariel-memory/.venv/bin/python3"
AGENTS = {
    "hermes": Path.home() / ".mcp-ariel-memory-hermes",
    "mimocode": Path.home() / ".mcp-ariel-memory-mimocode",
    "cowagent": Path.home() / ".mcp-ariel-memory-cowagent",
}

README = """# Skills SSOT

Shared skill set for all agents (D2.3). Every `.md` under `skill/` is synced
into each agent's ariel wiki (`wiki_type=skill`) with sha256 dedup — the SSOT
is the write surface, agents read copies.

Convention (D2.1): frontmatter with `title`, one skill per file, <4KB
(lint cap `skill_too_large`), API table at the top when applicable.
"""


def bootstrap() -> int:
    skill_dir = SSOT_DIR / "skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    readme = SSOT_DIR / "README.md"
    if not readme.exists():
        readme.write_text(README, encoding="utf-8")
    if not (SSOT_DIR / ".git").exists():
        subprocess.run([shutil.which("git") or "git", "init", "-q", str(SSOT_DIR)], check=True)
        print(f"SSOT initialized: {SSOT_DIR} (git repo)")
    else:
        print(f"SSOT exists: {SSOT_DIR}")
    return 0


def sync_one(agent: str) -> dict:
    """Run inside a per-agent subprocess (env already set by the parent)."""
    sys.path.insert(0, ARIEL_REPO)
    from wiki.manager import WikiManager

    async def _run() -> dict:
        wm = WikiManager(layer="user")
        res = await wm.sync_external([str(SSOT_DIR / "skill")])
        # aiosqlite workers are non-daemon: without close_all the interpreter
        # hangs at exit AFTER printing (autohooks _close_ariel lesson).
        from shared.connection import connection_manager

        await connection_manager.close_all()
        return res

    return asyncio.run(_run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync the skill SSOT into agent wikis")
    parser.add_argument("--agent", help="internal: sync one agent (parent sets env)")
    parser.add_argument("--bootstrap", action="store_true", help="create the SSOT skeleton")
    args = parser.parse_args(argv)

    if args.bootstrap:
        return bootstrap()

    if not (SSOT_DIR / "skill").is_dir():
        print(f"SSOT not found: {SSOT_DIR}/skill — run with --bootstrap first", file=sys.stderr)
        return 2

    if args.agent:
        results = sync_one(args.agent)
        print(f"{args.agent}: {results}")
        return 0

    failed = []
    for name, data_dir in AGENTS.items():
        if not data_dir.is_dir():
            print(f"{name}: skipped (no data dir {data_dir})")
            continue
        env = dict(os.environ, MCP_MEMORY_DATA_DIR=str(data_dir), ARIEL_HASH_EMBEDDINGS="1")
        proc = subprocess.run(
            [ARIEL_PY, __file__, "--agent", name],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        out = (proc.stdout or "").strip().splitlines()
        print(out[-1] if out else f"{name}: exit={proc.returncode} {(proc.stderr or '')[-120:]}")
        if proc.returncode != 0:
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
