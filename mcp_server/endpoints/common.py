import os
from typing import Any
from starlette.requests import Request
from features.rate_limiting import RateLimiter
from features.auth import bearer_auth
from config import config
from mcp_server.constants import AUTH_DISABLED_ENV, API_USER, UNKNOWN_USER


def check_auth(request: Request) -> bool:
    if os.environ.get(AUTH_DISABLED_ENV):
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
        return API_USER
    return request.client.host if request.client else UNKNOWN_USER


async def check_rate_limit(request: Request, api_rate_limiter: RateLimiter) -> bool:
    rate_enabled = config.get("features", "rate_limiting", default=True)
    if not rate_enabled:
        return True
    user = get_user_from_token(request)
    result = await api_rate_limiter.check(user)
    res: Any = result.get("allowed", True)
    return bool(res)
