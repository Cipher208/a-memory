"""HTTP surfaces for the external event dispatcher (spec S5).

Auth/rate-limit ride the shared helpers; isolation is inherited from the
per-agent instance (own process + MCP_MEMORY_DATA_DIR). This module resolves
mem/graph/rag via the layer registry (mcp_server-internal — no cycle) and
hands the resolved objects to the mcp_server-free dispatcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from mcp_server.constants import ERROR_RATE_LIMIT, ERROR_UNAUTHORIZED
from mcp_server.endpoints.common import check_auth, check_rate_limit

if TYPE_CHECKING:
    from features.rate_limiting import RateLimiter
    from starlette.requests import Request


def _resolve_mem_graph_rag(app_ctx: Any, layer: str, user_id: str) -> tuple[Any, Any, Any]:
    """Resolve layer backends once per request. rag=None when unavailable."""
    from mcp_server.tools.base import _get_graph, _get_memory, _get_rag

    mem = _get_memory(app_ctx, layer, user_id)
    graph = _get_graph(app_ctx, layer)
    try:
        rag = _get_rag(app_ctx, layer)
    except Exception:
        rag = None
    return mem, graph, rag


class HooksEndpoints:
    def __init__(self, app_ctx: Any, rate_limiter: RateLimiter | None):
        self.app_ctx = app_ctx
        self.rate_limiter = rate_limiter

    async def hooks_event(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if self.rate_limiter is not None and not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        event = request.path_params["event"]
        body = await request.json()
        from hooks.external import KNOWN_EVENTS, dispatch_event

        if event not in KNOWN_EVENTS:
            return JSONResponse({"error": f"unknown event: {event!r}. Must be one of {sorted(KNOWN_EVENTS)}"}, status_code=400)
        layer = body.get("layer", "user")
        user_id = body.get("user_id", "default")
        mem, graph, rag = _resolve_mem_graph_rag(self.app_ctx, layer, user_id)
        result = await dispatch_event(event, layer, user_id, body.get("payload", {}) or {}, mem, graph, rag)
        return JSONResponse({"event": event, "result": result})

    async def context_inject(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if self.rate_limiter is not None and not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        body = await request.json()
        from config import config

        from features.inject import build_inject_blocks

        budget = int(body.get("budget") or config.get("inject", "token_budget", default=2000))
        mem, _graph, rag = _resolve_mem_graph_rag(self.app_ctx, body.get("layer", "user"), body.get("user_id", "default"))
        blocks = await build_inject_blocks(
            mem,
            rag,
            body.get("user_id", "default"),
            text=body.get("text", ""),
            budget=budget,
        )
        return JSONResponse({"blocks": blocks, "budget": budget})
