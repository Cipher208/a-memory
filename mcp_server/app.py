from starlette.applications import Starlette
from starlette.routing import Mount, Route
from mcp.server.fastmcp import FastMCP

from mcp_server.context import AppContext
from mcp_server.middlewares import add_middlewares
from mcp_server.endpoints.dashboard import DashboardEndpoints
from mcp_server.endpoints.auth import AuthEndpoints
from mcp_server.endpoints.backup import BackupEndpoints
from mcp_server.endpoints.system import SystemEndpoints


def create_app(mcp: FastMCP, ctx: AppContext) -> Starlette:
    """Create Starlette app with modular endpoints."""
    # Instantiate endpoint handlers
    dashboard = DashboardEndpoints(ctx)
    auth = AuthEndpoints(ctx)
    backup = BackupEndpoints(ctx)
    system = SystemEndpoints(ctx)

    app = Starlette(
        routes=[
            Route("/health", system.health),
            Route("/ready", system.ready),
            Route("/alive", system.alive),
            Route("/dashboard", dashboard.page),
            Route("/api/stats", dashboard.api_stats),
            Route("/api/user/facts", dashboard.api_user_facts),
            Route("/api/agent/facts", dashboard.api_agent_facts),
            Route("/api/user/episodes", dashboard.api_user_episodes),
            Route("/api/agent/episodes", dashboard.api_agent_episodes),
            Route("/api/audit", dashboard.api_audit),
            Route("/api/auth/keys", auth.list_keys),
            Route("/api/auth/create", auth.create_key, methods=["POST"]),
            Route("/api/backup/trigger", backup.trigger, methods=["POST"]),
            Route("/api/backup/list", backup.list_backups),
            Route("/metrics", system.metrics_prometheus),
            Route("/metrics/json", system.metrics_json),
            Mount("/", app=mcp.streamable_http_app()),
        ],
    )

    add_middlewares(app)
    return app
