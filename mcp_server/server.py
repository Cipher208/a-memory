"""MCP Server — MCPServer setup, AppContext, lifespan, main()."""

import os
import sys
import logging
from pathlib import Path

from typing import Any, Literal
from mcp.server.mcpserver import MCPServer

# Ensure the root of the repo is in the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.lifespan import lifespan

mcp = MCPServer(
    "ariel-memory",
    instructions="Universal Two-Layer Memory MCP Server. Layer 1 (user) stores facts about users. Layer 2 (agent) stores agent identity, decisions, errors, and personality.",
    lifespan=lifespan,
)


STDIO_TRANSPORT: Literal["stdio"] = "stdio"


# Tools agents always see: the universal primitives. Everything else stays
# reachable through them (and ops via the dashboard HTTP surface).
PRIMITIVE_TOOLS = frozenset({"think", "dream", "forget", "evolve", "project"})


def _register_all_tools() -> None:
    import mcp_server.tools_layer  # noqa: F401 — populates the tool registry
    from mcp_server.registry import get_all_tools

    expose = os.environ.get("ARIEL_EXPOSE", "primitives").strip().lower()
    tools = get_all_tools()
    if expose != "all":
        hidden = sorted(set(tools) - PRIMITIVE_TOOLS)
        for name in hidden:
            del tools[name]

    for name, func in tools.items():
        mcp.tool(name=name)(func)


_register_all_tools()


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
    parser.add_argument("--dashboard", action="store_true", help="Enable dashboard + metrics endpoints")
    parser.add_argument("--no-auth", action="store_true", help="Disable auth for development")
    args = parser.parse_args()

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
