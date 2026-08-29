# autohooks/__main__.py
"""CLI: daemon | inject.

Import order is the contract: parse args → load config (ariel-free) → set env
→ THEN import ariel-side modules (appctx, daemon, inject).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from autohooks.config import AgentConfig, load_config


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="autohooks", description="Universal autohooks runtime (C1.9)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("daemon", "inject"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True, help="path to <agent>.yaml")
    sub.choices["inject"].add_argument("--text", default="", help="current message for relevance ranking")
    sub.choices["inject"].add_argument("--format", default="md", choices=["md", "json"])
    sub.choices["daemon"].add_argument("--once", action="store_true", help="one poll iteration (debug)")
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

    if ns.command == "daemon":
        from autohooks.daemon import run_daemon
        from autohooks.source import SqliteSource

        source = SqliteSource.from_config(cfg)
        asyncio.run(run_daemon(cfg, source, mem, graph, rag, max_iterations=1 if ns.once else None))
        return 0

    from autohooks.inject import run_inject

    out = asyncio.run(run_inject(cfg, mem, graph, rag, text=ns.text, fmt=ns.format))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
