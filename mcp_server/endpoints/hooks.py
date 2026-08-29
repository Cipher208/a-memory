"""HTTP surfaces for the external event dispatcher (spec S5).

Auth/rate-limit ride the shared helpers; isolation is inherited from the
per-agent instance (own process + MCP_MEMORY_DATA_DIR).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from mcp_server.constants import ERROR_RATE_LIMIT, ERROR_UNAUTHORIZED
from mcp_server.endpoints.common import check_auth, check_rate_limit

if TYPE_CHECKING:
    from features.rate_limiting import RateLimiter
    from starlette.requests import Request


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
        from hooks.external import dispatch_event

        try:
            result = await dispatch_event(
                self.app_ctx,
                event,
                body.get("layer", "user"),
                body.get("user_id", "default"),
                body.get("payload", {}) or {},
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
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
        blocks = await build_inject_blocks(
            self.app_ctx,
            body.get("layer", "user"),
            body.get("user_id", "default"),
            text=body.get("text", ""),
            budget=budget,
        )
        return JSONResponse({"blocks": blocks, "budget": budget})
