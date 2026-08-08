from __future__ import annotations

"""Tool registry — breaks circular imports between server.py, tools_layer.py, tools_ops.py.

Tools register themselves here. server.py pulls from here and applies @mcp.tool().
"""

from collections.abc import Callable
from typing import Any, TYPE_CHECKING


# if TYPE_CHECKING:
from mcp.server.fastmcp import Context

_tools: dict[str, Callable] = {}


def _get_ctx(ctx: Context | None) -> Any:
    """Extract AppContext from FastMCP lifespan context."""
    if ctx is None:
        raise ValueError("Context is required but was None")
    return ctx.request_context.lifespan_context


def register_tool(name: str, func: Callable) -> None:
    _tools[name] = func


def get_all_tools() -> dict[str, Callable]:
    return dict(_tools)
