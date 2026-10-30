from starlette.requests import Request
from starlette.responses import JSONResponse
from features.rate_limiting import RateLimiter
from mcp_server.endpoints.common import check_auth, check_rate_limit
from mcp_server.constants import (
    ERROR_UNAUTHORIZED,
    ERROR_RATE_LIMIT,
    ERROR_KEY_STORE_UNREADABLE,
    DEFAULT_USER,
)


class AuthEndpoints:
    def __init__(self, rate_limiter: RateLimiter):
        self.rate_limiter = rate_limiter

    async def auth_keys(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        from features.auth import api_key_auth
        from features.auth.store import StoreUnreadable

        try:
            keys = api_key_auth.list_keys()
        except StoreUnreadable:
            return JSONResponse({"error": ERROR_KEY_STORE_UNREADABLE}, status_code=503)

        return JSONResponse(keys)

    async def auth_create(self, request: Request) -> JSONResponse:
        if not check_auth(request):
            return JSONResponse({"error": ERROR_UNAUTHORIZED}, status_code=401)
        if not await check_rate_limit(request, self.rate_limiter):
            return JSONResponse({"error": ERROR_RATE_LIMIT}, status_code=429)
        from features.auth import api_key_auth
        from features.auth.store import StoreUnreadable

        body = await request.json()
        try:
            key = api_key_auth.create_key(body.get("user_id", DEFAULT_USER), body.get("label", ""))
        except StoreUnreadable:
            return JSONResponse({"error": ERROR_KEY_STORE_UNREADABLE}, status_code=503)
        return JSONResponse({"api_key": key})
