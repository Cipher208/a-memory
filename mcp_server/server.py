"""MCP Server — MCPServer setup, AppContext, lifespan, main()."""

import functools
import inspect
import os
from collections.abc import Callable
import sys
import logging
from pathlib import Path

from typing import Any, Literal
from mcp.server.mcpserver import MCPServer

# Ensure the root of the repo is in the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.lifespan import lifespan
from mcp_server.tools.base import _resolve_user_id

mcp = MCPServer(
    "ariel-memory",
    instructions="Universal Two-Layer Memory MCP Server. Layer 1 (user) stores facts about users. Layer 2 (agent) stores agent identity, decisions, errors, and personality.",
    lifespan=lifespan,
)


STDIO_TRANSPORT: Literal["stdio"] = "stdio"


# Tools agents always see: the universal primitives. Everything else stays
# reachable through them (and ops via the dashboard HTTP surface).
PRIMITIVE_TOOLS = frozenset({"think", "dream", "forget", "evolve", "project", "memory_hook"})


EXTRA_TIERS: dict[str, Callable[[str, set[str]], set[str]]] = {
    # tier name -> matcher(tool_name, all_names) returning extra tools to expose
    "wiki": lambda name, names: {n for n in names if n.startswith("wiki_")},
    "brief": lambda name, names: {n for n in names if n.startswith("daily_")},
    # review: staged-mutation review surface (C1.11/C1.13/C1.14) — opt-in per instance
    "review": lambda name, names: names & {"memory_proposals", "memory_report_card"},
    # Phase D coherent groups:
    # context = build/recover context (recall protocol, recap, budgets, steering, compression)
    "context": lambda name, names: (
        names
        & {
            "memory_recall_protocol",
            "memory_recap",
            "memory_get_smart_context",
            "memory_context",
            "memory_context_inject",
            "memory_steering",
            "memory_compress",
        }
    ),
    # insight = read-side analytics (query DSL, provenance blame, quality, reflections, raw search)
    "insight": lambda name, names: (
        names
        & {
            "memory_query",
            "memory_fact_blame",
            "memory_quality",
            "memory_reflect",
            "memory_stats",
            "memory_search",
            "memory_recall",
            "memory_episode_recall",
            "memory_episode_list",
            "memory_episode_get",
            "memory_session_list",
            "memory_graph_query",
            "memory_graph_nodes",
            "memory_graph_edges",
        }
    ),
    # write = shape memory (typed saves, rules, scratchpad, counterfactuals, episodes, graph, sessions)
    "write": lambda name, names: (
        names
        & {
            "memory_remember",
            "memory_save_typed",
            "memory_load_rules",
            "memory_scratchpad",
            "memory_counterfactual",
            "memory_episode_save",
            "memory_graph_add",
            "memory_session_start",
            "memory_session_end",
        }
    ),
}


def resolve_exposure(expose: str, all_names: set[str]) -> set[str]:
    """Resolve an ARIEL_EXPOSE value to the exposed tool-name set.

    Formats: 'primitives' (default), 'all', or comma-separated tiers
    e.g. 'primitives,wiki'. Unknown tiers are ignored.
    """
    allowed = set(PRIMITIVE_TOOLS) & all_names
    for tier in (t.strip() for t in expose.split(",") if t.strip()):
        matcher = EXTRA_TIERS.get(tier)
        if matcher:
            allowed |= matcher(tier, all_names)
    return allowed


def _scope_tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """Bind the user_id kwarg to the authenticated API key when present.

    functools.wraps keeps `__wrapped__`, so mcp 2.x resolves type
    annotations (e.g. Literal[...]) in the original module via
    inspect.signature(func, eval_str=True).
    """
    sig = inspect.signature(func)
    if "user_id" not in sig.parameters:
        return func

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if "user_id" in kwargs:
            kwargs["user_id"] = _resolve_user_id(kwargs.get("ctx"), kwargs["user_id"])
        return await func(*args, **kwargs)

    return wrapper


def _register_all_tools() -> None:
    import mcp_server.tools_layer  # noqa: F401 — populates the tool registry
    from mcp_server.registry import get_all_tools

    expose = os.environ.get("ARIEL_EXPOSE", "primitives").strip().lower()
    tools = get_all_tools()
    total = len(tools)
    if expose != "all":
        allowed = resolve_exposure(expose, set(tools))
        hidden = sorted(set(tools) - allowed)
        for name in hidden:
            del tools[name]

    for name, func in tools.items():
        mcp.tool(name=name)(_scope_tool(func))

    # Startup evidence for the env-sanitization gotcha: MCP stdio clients pass
    # a sanitized environment, so a shell-profile ARIEL_EXPOSE silently never
    # reaches the server. Log the resolved surface so misconfiguration shows
    # up in logs instead of an agent reporting "I only see 6 tools".
    global _EXPOSURE_SUMMARY
    _EXPOSURE_SUMMARY = (len(tools), total, expose)


_EXPOSURE_SUMMARY: tuple[int, int, str] | None = None

_register_all_tools()


def _setup_logging() -> None:
    """Configure logging from the logging.* section of config.yaml."""
    import logging.handlers

    from config import config

    level = str(config.get("logging", "level", default="INFO")).upper()
    max_mb = int(config.get("logging", "max_size_mb", default=10))
    backups = int(config.get("logging", "backup_count", default=5))
    rel_file = str(config.get("logging", "file", default="memory.log"))
    data_dir = os.environ.get("MCP_MEMORY_DATA_DIR", os.path.expanduser("~/.mcp-ariel-memory"))
    log_dir = os.path.join(data_dir, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(os.path.join(log_dir, rel_file), maxBytes=max_mb * 1024 * 1024, backupCount=backups)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(getattr(logging, level, logging.INFO))
        root.addHandler(handler)
    except OSError:
        pass  # unwritable dir — console logging still works

    # _EXPOSURE_SUMMARY is captured at import (before logging is configured) —
    # emit it here so the resolved tool surface lands in memory.log.
    if _EXPOSURE_SUMMARY is not None:
        exposed, total, expose = _EXPOSURE_SUMMARY
        logging.getLogger(__name__).info("tool exposure: %d/%d tools (ARIEL_EXPOSE=%s)", exposed, total, expose)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Ariel Memory MCP Server")
    parser.add_argument(
        "--transport",
        choices=[STDIO_TRANSPORT, "http"],
        default=STDIO_TRANSPORT,
        help="Transport: stdio (Claude Desktop) or http (web clients)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP host (default: 0.0.0.0)")  # noqa: S104
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Enable dashboard + metrics endpoints (default: dashboard.enabled from config.yaml)",
    )
    parser.add_argument("--no-auth", action="store_true", help="Disable auth for development")
    args = parser.parse_args()

    # CLI flag OR config — they synchronize instead of fighting.
    if not args.dashboard:
        from config import config as _cfg

        args.dashboard = bool(_cfg.get("dashboard", "enabled", default=False))
    if args.port == 8000:
        from config import config as _cfg

        args.port = int(_cfg.get("dashboard", "port", default=8000))

    _setup_logging()

    if args.no_auth:
        os.environ["MCP_AUTH_DISABLED"] = "1"

    if args.transport == "http":
        if args.dashboard:
            _run_with_dashboard(args.host, args.port)
        else:
            try:
                # mcp 2.x: host/port are run() kwargs, not Settings fields
                mcp.run(transport="streamable-http", host=args.host, port=args.port)
            except Exception:
                logging.getLogger(__name__).exception("HTTP transport failed. Try with --dashboard flag.")
                raise
    else:
        mcp.run(transport=STDIO_TRANSPORT)


def _run_with_dashboard(host: str, port: int) -> None:
    import uvicorn
    from mcp_server.app import create_app
    from mcp_server.context import AppContext

    ctx = AppContext()
    app = create_app(mcp, ctx)

    _setup_shutdown_signals(_shutdown_handler)

    uvicorn.run(app, host=host, port=port)


def _setup_shutdown_signals(handler: Any) -> None:
    import signal

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _shutdown_handler(signum: int, _frame: Any) -> None:
    from features.backup_cron import backup_cron
    from shared.read_only import read_only_replica
    from shared.saga import saga_watchdog

    backup_cron.stop()
    saga_watchdog.stop()
    read_only_replica.stop()
    os._exit(0)


if __name__ == "__main__":
    main()
