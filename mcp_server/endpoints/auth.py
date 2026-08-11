from starlette.requests import Request
from starlette.responses import JSONResponse
from features.rate_limiting import RateLimiter
from mcp_server.endpoints.common import check_auth, check_rate_limit
from mcp_server.constants import ERROR_UNAUTHORIZED, ERROR_RATE_LIMIT, DEFAULT_USER


class AuthEndpoints:
    def __init__(self, rate_limiter: RateLimiter):
        self.rate_limiter = rate_limiter

    async def auth_keys(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        from features.auth import api_key_auth

        return JSONResponse(api_key_auth.list_keys())

    async def auth_create(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        from features.auth import api_key_auth

        body = await request.json()
        key = api_key_auth.create_key(body.get("user_id", DEFAULT_USER), body.get("label", ""))
        return JSONResponse({"api_key": key})
