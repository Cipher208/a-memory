"""MCPServer Application Factory and Route Orchestrator.

Decomposes the server logic into a Starlette-based application structure,
managing API endpoints, health checks, and dashboard rendering.
"""

from starlette.applications import Starlette
from starlette.routing import Mount, Route
from mcp.server.mcpserver import MCPServer

from mcp_server.context import AppContext
from mcp_server.middlewares import add_middlewares
from mcp_server.endpoints.dashboard import DashboardEndpoints
from mcp_server.endpoints.auth import AuthEndpoints
from mcp_server.endpoints.backup import BackupEndpoints
from mcp_server.endpoints.system import SystemEndpoints
from mcp_server.endpoints.hooks import HooksEndpoints


def create_app(mcp: MCPServer, ctx: AppContext) -> Starlette:
    """Create Starlette app with modular endpoints."""
    # Instantiate internal handlers first
    from features.dashboard import Dashboard
    from features.rate_limiting import RateLimiter

    dash_logic = Dashboard(mm=ctx.mm)
    api_limiter = RateLimiter()

    # Instantiate endpoint handlers
    dashboard = DashboardEndpoints(dash_logic, api_limiter)
    auth = AuthEndpoints(api_limiter)
    backup = BackupEndpoints(api_limiter)
    system = SystemEndpoints(api_limiter)
    hooks = HooksEndpoints(ctx, api_limiter)

    app = Starlette(
        routes=[
            Route("/health", system.health_endpoint),
            Route("/ready", system.ready_endpoint),
            Route("/alive", system.alive_endpoint),
            Route("/dashboard", dashboard.dashboard_page),
            Route("/api/stats", dashboard.api_stats),
            Route("/api/user/facts", dashboard.api_user_facts),
            Route("/api/agent/facts", dashboard.api_agent_facts),
            Route("/api/user/episodes", dashboard.api_user_episodes),
            Route("/api/agent/episodes", dashboard.api_agent_episodes),
            Route("/api/audit", dashboard.api_audit),
            Route("/api/auth/keys", auth.auth_keys),
            Route("/api/auth/create", auth.auth_create, methods=["POST"]),
            Route("/api/backup/trigger", backup.backup_trigger, methods=["POST"]),
            Route("/api/backup/list", backup.backup_list),
            Route("/api/hooks/{event}", hooks.hooks_event, methods=["POST"]),
            Route("/api/context-inject", hooks.context_inject, methods=["POST"]),
            Route("/metrics", system.metrics_endpoint),
            Route("/metrics/json", system.metrics_json),
            Mount("/", app=mcp.streamable_http_app()),
        ],
    )

    add_middlewares(app)
    return app
