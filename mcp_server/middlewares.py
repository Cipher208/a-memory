import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from features.auth import bearer_auth
from config import config

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in ("/mcp", "/health", "/ready", "/alive"):
            return await call_next(request)
        if os.environ.get("MCP_AUTH_DISABLED"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth and not bearer_auth.verify(auth):
            return JSONResponse({"error": "Invalid token"}, status_code=401)
        return await call_next(request)

def add_middlewares(app):
    app.add_middleware(AuthMiddleware)
    allowed_origins = config.get("cors", "allowed_origins", default=["http://localhost:*", "http://127.0.0.1:*"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "DELETE"],
        expose_headers=["Mcp-Session-Id"],
    )
