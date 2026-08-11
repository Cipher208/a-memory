from __future__ import annotations

"""Tool registry — breaks circular imports between server.py, tools_layer.py, tools_ops.py.

Tools register themselves here. server.py pulls from here and applies @mcp.tool().
"""

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context
    from collections.abc import Callable

# if TYPE_CHECKING:


_tools: dict[str, Callable[..., Any]] = {}


def _get_ctx(ctx: Context[Any, Any] | None) -> Any:
    """Extract AppContext from FastMCP lifespan context."""
    if ctx is None:
        raise ValueError("Context is required but was None")
    return ctx.request_context.lifespan_context


def register_tool(name: str, func: Callable[..., Any]) -> None:
    _tools[name] = func


def get_all_tools() -> dict[str, Callable[..., Any]]:
    return dict(_tools)
