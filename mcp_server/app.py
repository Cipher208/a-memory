import os
import time as _time
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from mcp.server.fastmcp import FastMCP

from mcp_server.context import AppContext
from mcp_server.middlewares import add_middlewares
from features.dashboard import Dashboard
from features.rate_limiting import RateLimiter
from shared.metrics import metrics as m
from features.auth import bearer_auth
from config import config

_server_start_time = _time.time()


def check_auth(request: Request) -> bool:
    if os.environ.get("MCP_AUTH_DISABLED"):
        return True
    auth_enabled = config.get("auth", "bearer_token_enabled", default=True)
    if not auth_enabled:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth:
        return False
    return bearer_auth.verify(auth)


def get_user_from_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and bearer_auth.verify(auth):
        return "api"
    return request.client.host if request.client else "unknown"


async def check_rate_limit(request: Request, api_rate_limiter: RateLimiter) -> bool:
    rate_enabled = config.get("features", "rate_limiting", default=True)
    if not rate_enabled:
        return True
    user = get_user_from_token(request)
    result = await api_rate_limiter.check(user)
    return result.get("allowed", True)


def create_app(mcp: FastMCP, ctx: AppContext) -> Starlette:
    dashboard = Dashboard(mm=ctx.mm)
    api_rate_limiter = RateLimiter()

    async def dashboard_page(request: Request) -> Response:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        return HTMLResponse(dashboard.render_html())

    async def api_stats(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        user_id = request.query_params.get("user_id", "default")
        return JSONResponse(await dashboard.get_stats(user_id))

    async def api_user_facts(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        user_id = request.query_params.get("user_id", "default")
        return JSONResponse(await dashboard.get_user_facts(user_id))

    async def api_agent_facts(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        user_id = request.query_params.get("user_id", "default")
        return JSONResponse(await dashboard.get_agent_facts(user_id))

    async def api_user_episodes(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        user_id = request.query_params.get("user_id", "default")
        return JSONResponse(await dashboard.get_user_episodes(user_id))

    async def api_agent_episodes(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        user_id = request.query_params.get("user_id", "default")
        return JSONResponse(await dashboard.get_agent_episodes(user_id))

    async def api_audit(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        return JSONResponse(await dashboard.get_audit())

    async def metrics_endpoint(request: Request) -> Response:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        return PlainTextResponse(m.render_prometheus())

    async def metrics_json(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        return JSONResponse(m.render_json())

    async def auth_keys(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        from features.auth import api_key_auth

        return JSONResponse(api_key_auth.list_keys())

    async def auth_create(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        from features.auth import api_key_auth

        body = await request.json()
        key = api_key_auth.create_key(body.get("user_id", "default"), body.get("label", ""))
        return JSONResponse({"api_key": key})

    async def backup_trigger(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        from features.backup_cron import backup_cron

        path = backup_cron.backup_now()
        return JSONResponse({"path": path})

    async def backup_list(request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if not await check_rate_limit(request, api_rate_limiter):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        from features.backup_cron import backup_cron

        return JSONResponse(backup_cron.list_backups())

    async def health_endpoint(request: Request) -> JSONResponse:
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

    async def ready_endpoint(request: Request) -> JSONResponse:
        from shared.migrations import migration_manager

        try:
            current = await migration_manager.get_current_version()
            ready = False
            if isinstance(current, (int, float)):
                ready = current >= 2
        except Exception:
            ready = False
        return JSONResponse({"ready": ready, "migration_version": current if isinstance(current, (int, float)) else 0})

    async def alive_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"alive": True})

    app = Starlette(
        routes=[
            Route("/health", health_endpoint),
            Route("/ready", ready_endpoint),
            Route("/alive", alive_endpoint),
            Route("/dashboard", dashboard_page),
            Route("/api/stats", api_stats),
            Route("/api/user/facts", api_user_facts),
            Route("/api/agent/facts", api_agent_facts),
            Route("/api/user/episodes", api_user_episodes),
            Route("/api/agent/episodes", api_agent_episodes),
            Route("/api/audit", api_audit),
            Route("/api/auth/keys", auth_keys),
            Route("/api/auth/create", auth_create, methods=["POST"]),
            Route("/api/backup/trigger", backup_trigger, methods=["POST"]),
            Route("/api/backup/list", backup_list),
            Route("/metrics", metrics_endpoint),
            Route("/metrics/json", metrics_json),
            Mount("/", app=mcp.streamable_http_app()),
        ],
    )

    add_middlewares(app)
    return app
