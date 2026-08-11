from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from features.dashboard import Dashboard
from features.rate_limiting import RateLimiter
from mcp_server.endpoints.common import check_auth, check_rate_limit
from mcp_server.constants import ERROR_UNAUTHORIZED, ERROR_RATE_LIMIT, DEFAULT_USER


class DashboardEndpoints:
    def __init__(self, dashboard: Dashboard, rate_limiter: RateLimiter):
        self.dashboard = dashboard
        self.rate_limiter = rate_limiter

    async def dashboard_page(self, request: Request) -> Response:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        return HTMLResponse(self.dashboard.render_html())

    async def api_stats(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        user_id = request.query_params.get("user_id", DEFAULT_USER)
        return JSONResponse(await self.dashboard.get_stats(user_id))

    async def api_user_facts(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        user_id = request.query_params.get("user_id", DEFAULT_USER)
        return JSONResponse(await self.dashboard.get_user_facts(user_id))

    async def api_agent_facts(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        user_id = request.query_params.get("user_id", DEFAULT_USER)
        return JSONResponse(await self.dashboard.get_agent_facts(user_id))

    async def api_user_episodes(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        user_id = request.query_params.get("user_id", DEFAULT_USER)
        return JSONResponse(await self.dashboard.get_user_episodes(user_id))

    async def api_agent_episodes(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        user_id = request.query_params.get("user_id", DEFAULT_USER)
        return JSONResponse(await self.dashboard.get_agent_episodes(user_id))

    async def api_audit(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        return JSONResponse(await self.dashboard.get_audit())
