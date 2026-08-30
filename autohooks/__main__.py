# autohooks/__main__.py
"""CLI: daemon | inject | dispatch.

Import order is the contract: parse args → load config (ariel-free) → set env
→ THEN import ariel-side modules (appctx, daemon, inject, dispatch).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from autohooks.config import AgentConfig, load_config


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="autohooks", description="Universal autohooks runtime (C1.9)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("daemon", "inject", "dispatch", "recall", "recap"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True, help="path to <agent>.yaml")
    sub.choices["inject"].add_argument("--text", default="", help="current message for relevance ranking")
    sub.choices["inject"].add_argument("--format", default="md", choices=["md", "json"])
    sub.choices["inject"].add_argument("--blocks", default="", help="comma-separated block kinds to keep (empty = all)")
    sub.choices["recall"].add_argument("--query", default="", help="recall query (empty = zero-state)")
    sub.choices["recall"].add_argument("--budget", default="2000", help="token budget")
    sub.choices["recall"].add_argument("--format", default="md", choices=["md", "json"])
    sub.choices["recap"].add_argument("--budget", default="2000", help="token budget")
    sub.choices["recap"].add_argument("--format", default="md", choices=["md", "json"])
    sub.choices["daemon"].add_argument("--once", action="store_true", help="one poll iteration (debug)")
    sub.choices["dispatch"].add_argument("--event", required=True, help="KNOWN_EVENTS name to fire")
    sub.choices["dispatch"].add_argument("--since", default="0", help="since timestamp for diff-style events")
    sub.choices["dispatch"].add_argument("--until", default="0", help="until timestamp for diff-style events")
    sub.choices["dispatch"].add_argument("--payload", default="{}", help="JSON object merged into the event context")
    return parser.parse_args(argv)


def apply_env(cfg: AgentConfig) -> None:
    """Set the ariel env BEFORE any ariel import. Isolation rides MCP_MEMORY_DATA_DIR."""
    os.environ["MCP_MEMORY_DATA_DIR"] = str(cfg.data_dir)
    os.environ.setdefault("MCP_CONFIG_PATH", str(cfg.data_dir / "config.yaml"))
    if cfg.master_key:
        os.environ["MCP_MASTER_KEY"] = cfg.master_key


logger = logging.getLogger("autohooks.cli")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ns = _parse_args(argv)
    cfg_path = Path(ns.config)
    if not cfg_path.exists():
        logger.error("config not found: %s", cfg_path)
        return 2
    cfg = load_config(cfg_path)

    if not cfg.data_dir.exists():
        logger.error("data_dir does not exist: %s (is the agent's ariel instance provisioned?)", cfg.data_dir)
        return 2
    if not cfg.source.path.exists():
        logger.error("source DB does not exist: %s", cfg.source.path)
        return 2

    apply_env(cfg)

    # ariel imports happen ONLY after apply_env.
    from autohooks.appctx import build_app_context, resolve_layer

    app = build_app_context()
    mem, graph, rag = resolve_layer(app, cfg.layer, cfg.user_id)

    def _close_ariel() -> None:
        """Close ariel's aiosqlite connections before exit.

        Without this the non-daemon aiosqlite worker threads block interpreter
        shutdown and the CLI hangs after printing its output (found via
        faulthandler during C1.10 e2e; exit codes were masked by pipes).
        """
        from shared.connection import connection_manager

        asyncio.run(connection_manager.close_all())

    if ns.command == "daemon":
        from autohooks.daemon import run_daemon
        from autohooks.source import SqliteSource

        source = SqliteSource.from_config(cfg)
        asyncio.run(run_daemon(cfg, source, mem, graph, rag, max_iterations=1 if ns.once else None))
        _close_ariel()
        return 0

    if ns.command == "dispatch":
        from hooks.external import dispatch_event

        try:
            extra = json.loads(ns.payload) if ns.payload else {}
        except json.JSONDecodeError:
            logger.exception("invalid --payload JSON")
            return 2
        if not isinstance(extra, dict):
            logger.error("--payload must be a JSON object")
            return 2
        since = float(ns.since) if ns.since else 0.0
        until = float(ns.until) if ns.until else 0.0
        result = asyncio.run(
            dispatch_event(
                ns.event,
                cfg.layer,
                cfg.user_id,
                {"since": since, "until": until, **extra},
                mem,
                graph,
                rag,
            )
        )
        _close_ariel()
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if ns.command == "recall":
        from features.recall import recall_protocol

        blocks = asyncio.run(recall_protocol(mem, rag, cfg.user_id, query=ns.query, budget=int(ns.budget)))
        _close_ariel()
        if ns.format == "json":
            print(json.dumps({"blocks": blocks}, ensure_ascii=False))
        else:
            print(_render_recall_md(blocks))
        return 0

    if ns.command == "recap":
        from features.continuity import session_recap

        blocks = asyncio.run(session_recap(mem, cfg.user_id, budget=int(ns.budget)))
        _close_ariel()
        if ns.format == "json":
            print(json.dumps({"blocks": blocks}, ensure_ascii=False))
        else:
            print(_render_recall_md(blocks))
        return 0

    from autohooks.inject import run_inject

    out = asyncio.run(run_inject(cfg, mem, graph, rag, text=ns.text, fmt=ns.format, blocks=ns.blocks))
    _close_ariel()
    print(out)
    return 0


def _render_recall_md(blocks: list[dict[str, Any]]) -> str:
    if not blocks:
        return "—"
    lines = [f"- [{b['axis']}] {b['content']}" for b in blocks if str(b.get("content", "")).strip()]
    return "\n".join(lines) if lines else "—"


if __name__ == "__main__":
    sys.exit(main())
