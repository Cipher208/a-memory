"""Universal Primitives — the five tools every MCP client sees.

Split by primitive; this package re-exports the public tool functions
(and ``_auto_route`` for tests) so ``from ..primitives import think``
keeps working exactly as when everything lived in one module.
"""

from mcp_server.tools.primitives.routing import _auto_route
from mcp_server.tools.primitives.think import think
from mcp_server.tools.primitives.dream import dream
from mcp_server.tools.primitives.forget import forget
from mcp_server.tools.primitives.evolve import evolve
from mcp_server.tools.primitives.project import project

__all__ = [
    "_auto_route",
    "dream",
    "evolve",
    "forget",
    "project",
    "think",
]
