# autohooks/appctx.py
"""Build the ariel AppContext and resolve layer bindings outside the server (spec S2).

MCP_MEMORY_DATA_DIR / MCP_CONFIG_PATH / MCP_MASTER_KEY must be set BEFORE this
module is imported — the CLI guarantees that; tests rely on conftest env.
Isolation is inherited: each agent's daemon process points at its own data dir.
"""

from __future__ import annotations

from typing import Any

from mcp_server.context import AppContext

__all__ = ["build_app_context", "resolve_layer"]


def build_app_context() -> AppContext:
    """Build the same AppContext the server performs at startup (zero-arg; env-driven)."""
    return AppContext()


def resolve_layer(app: AppContext, layer: str, user_id: str) -> tuple[Any, Any, Any]:
    """Resolve (mem, graph, rag) exactly like the HTTP hooks endpoint does."""
    from mcp_server.tools.base import _get_graph, _get_memory, _get_rag

    mem = _get_memory(app, layer, user_id)
    graph = _get_graph(app, layer)
    try:
        rag = _get_rag(app, layer)
    except Exception:
        rag = None
    return mem, graph, rag
