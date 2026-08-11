import time as _time
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from features.rate_limiting import RateLimiter
from shared.metrics import metrics as m
from mcp_server.endpoints.common import check_auth, check_rate_limit
from mcp_server.constants import ERROR_UNAUTHORIZED, ERROR_RATE_LIMIT

_server_start_time = _time.time()


class SystemEndpoints:
    def __init__(self, rate_limiter: RateLimiter):
        self.rate_limiter = rate_limiter

    async def health_endpoint(self, request: Request) -> JSONResponse:
        from shared.connection import connection_manager

        start = _time.time()
        try:
            conn = await connection_manager.get("memory.db")
            await (await conn.execute("SELECT 1")).fetchone()
            db_ok = True
        except Exception:
            db_ok = False
        db_latency = _time.time() - start
        status = "ok" if db_ok else "degraded"
        return JSONResponse(
            {
                "status": status,
                "version": "1.0.0",
                "uptime_seconds": _time.time() - _server_start_time,
                "db": {"connected": db_ok, "latency_ms": round(db_latency * 1000, 1)},
            }
        )

    async def ready_endpoint(self, request: Request) -> JSONResponse:
        from shared.migrations import migration_manager

        try:
            current = await migration_manager.get_current_version()
            ready = False
            if isinstance(current, (int, float)):
                ready = current >= 2
        except Exception:
            ready = False
        return JSONResponse({"ready": ready, "migration_version": current if isinstance(current, (int, float)) else 0})

    async def alive_endpoint(self, request: Request) -> JSONResponse:
        return JSONResponse({"alive": True})

    async def metrics_endpoint(self, request: Request) -> Response:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        return PlainTextResponse(m.render_prometheus())

    async def metrics_json(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        return JSONResponse(m.render_json())
