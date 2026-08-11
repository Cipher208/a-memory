from starlette.requests import Request
from starlette.responses import JSONResponse
from features.rate_limiting import RateLimiter
from mcp_server.endpoints.common import check_auth, check_rate_limit
from mcp_server.constants import ERROR_UNAUTHORIZED, ERROR_RATE_LIMIT


class BackupEndpoints:
    def __init__(self, rate_limiter: RateLimiter):
        self.rate_limiter = rate_limiter

    async def backup_trigger(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        from features.backup_cron import backup_cron

        path = backup_cron.backup_now()
        return JSONResponse({"path": path})

    async def backup_list(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        from features.backup_cron import backup_cron

        return JSONResponse(backup_cron.list_backups())
