"""MCP Server — FastMCP setup, AppContext, lifespan, main()."""

import asyncio
import os
import sys
import time as _time
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_server_start_time = _time.time()

_data_dir = os.environ.get('MCP_MEMORY_DATA_DIR', str(Path.home() / '.mcp-ariel-memory'))
os.environ.setdefault('MCP_MEMORY_DATA_DIR', _data_dir)

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import config
from core import MemoryManager
from features.audit_trail import AuditTrail
from features.auth import bearer_auth
from features.backup import BackupManager
from features.backup_cron import backup_cron
from features.import_export import ImportExport
from features.rate_limiting import RateLimiter
from graph.epistemic import EpistemicGraph
from graph.temporal import TemporalGraph
from hooks.agent_hooks import AgentHooks
from hooks.user_hooks import UserHooks
from lifecycle.consolidation import ConsolidationEngine
from lifecycle.emotion import EmotionTrigger, EmotionEngine, load_emotion_config
from lifecycle.forgetting import ForgettingSystem
from lifecycle.importance_scheduler import importance_scheduler
from rag.engine import RAGEngine
from rag.multi_source import MultiSourceRAG
from shared.cache import MemoryCache
from shared.read_only import read_only_replica
from wiki import WikiManager


class AppContext:
    def __init__(self):
        self.cache = MemoryCache()
        self.mm = MemoryManager(cache=self.cache)
        self.user_wiki = WikiManager(layer='user')
        self.agent_wiki = WikiManager(layer='agent')
        self.user_rag = RAGEngine(layer='user')
        self.agent_rag = RAGEngine(layer='agent')
        self.user_multi = MultiSourceRAG(self.user_rag, self.user_wiki)
        self.agent_multi = MultiSourceRAG(self.agent_rag, self.agent_wiki)
        self.user_graph = EpistemicGraph(layer='user')
        self.agent_graph = EpistemicGraph(layer='agent')
        self.temporal = TemporalGraph()
        self.forgetting = ForgettingSystem()
        
        self.emotion_config = load_emotion_config()
        self.emotion_engine = EmotionEngine(config=self.emotion_config)
        self.emotion_trigger = EmotionTrigger(self.emotion_engine)
        
        self.consolidation = ConsolidationEngine()
        self.audit = AuditTrail()
        self.rate_limiter = RateLimiter()
        self.backup = BackupManager()
        self.import_export = ImportExport()

        from hooks import hook_registry
        self.hook_registry = hook_registry

        self.user_hooks = UserHooks()
        self.agent_hooks = AgentHooks()
        self.hook_registry.register_instance(self.user_hooks)
        self.hook_registry.register_instance(self.agent_hooks)


@asynccontextmanager
async def lifespan(server: FastMCP):
    from shared.migrations import migration_manager

    result = await migration_manager.migrate()
    logging.getLogger(__name__).info(f'Migrations: {result}')

    await asyncio.to_thread(read_only_replica.sync)

    ctx = AppContext()

    async def _delayed_start():
        await asyncio.sleep(5)
        backup_cron.start()
        importance_scheduler.start()
        logging.getLogger(__name__).info('Background tasks started after delay')

    asyncio.create_task(_delayed_start())

    async def _periodic_tasks():
        from lifecycle.forgetting import ForgettingSystem
        forgetting = ForgettingSystem()
        while True:
            try:
                await asyncio.sleep(900)  # 15 minutes
                await forgetting.cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.getLogger(__name__).exception('Periodic task error: %s', e)

    periodic_task = asyncio.create_task(_periodic_tasks())
    try:
        yield ctx
    finally:
        periodic_task.cancel()
        with suppress(asyncio.CancelledError):
            await periodic_task
        importance_scheduler.stop()
        backup_cron.stop()


mcp = FastMCP(
    'ariel-memory',
    instructions='Universal Two-Layer Memory MCP Server. Layer 1 (user) stores facts about users. Layer 2 (agent) stores agent identity, decisions, errors, and personality.',
    lifespan=lifespan,
)


def _register_all_tools():
    import mcp_server.tools_layer
    import mcp_server.tools_ops  # noqa: F401
    from mcp_server.registry import get_all_tools

    for name, func in get_all_tools().items():
        mcp.tool(name=name)(func)


_register_all_tools()


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Ariel Memory MCP Server')
    parser.add_argument(
        '--transport',
        choices=['stdio', 'http'],
        default='stdio',
        help='Transport: stdio (Claude Desktop) or http (web clients)',
    )
    parser.add_argument('--host', default='0.0.0.0', help='HTTP host (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8000, help='HTTP port (default: 8000)')
    parser.add_argument('--dashboard', action='store_true', help='Enable dashboard + metrics endpoints')
    parser.add_argument('--no-auth', action='store_true', help='Disable auth for development')
    args = parser.parse_args()

    if args.no_auth:
        os.environ['MCP_AUTH_DISABLED'] = '1'

    if args.transport == 'http':
        if args.dashboard:
            _run_with_dashboard(args.host, args.port)
        else:
            try:
                mcp.settings.host = args.host
                mcp.settings.port = args.port
                mcp.run(transport='streamable-http')
            except Exception as e:
                logging.getLogger(__name__).exception('HTTP transport failed: %s. Try with --dashboard flag.', e)
                raise
    else:
        mcp.run(transport='stdio')


def _run_with_dashboard(host: str, port: int):
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route

    from features.dashboard import Dashboard
    from features.rate_limiting import ConnectionLimiter, RateLimiter
    from shared.metrics import metrics as m

    ctx = AppContext()

    async def _delayed_start():
        await asyncio.sleep(5)
        backup_cron.start()
        importance_scheduler.start()
        logging.getLogger(__name__).info('Background tasks started after delay')

    asyncio.create_task(_delayed_start())
    dashboard = Dashboard(mm=ctx.mm)
    api_rate_limiter = RateLimiter()
    ws_limiter = ConnectionLimiter()

    def check_auth(request) -> bool:
        if os.environ.get('MCP_AUTH_DISABLED'):
            return True
        auth_enabled = config.get('auth', 'bearer_token_enabled', default=True)
        if not auth_enabled:
            return True
        auth = request.headers.get('Authorization', '')
        if not auth:
            return False
        return bearer_auth.verify(auth)

    def get_user_from_token(request) -> str:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer ') and bearer_auth.verify(auth):
            return 'api'
        return request.client.host if request.client else 'unknown'

    async def check_rate_limit(request) -> bool:
        rate_enabled = config.get('features', 'rate_limiting', default=True)
        if not rate_enabled:
            return True
        user = get_user_from_token(request)
        result = await api_rate_limiter.check(user)
        return result.get('allowed', True)

    async def dashboard_page(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        return HTMLResponse(dashboard.render_html())

    async def api_stats(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        user_id = request.query_params.get('user_id', 'default')
        return JSONResponse(await dashboard.get_stats(user_id))

    async def api_user_facts(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        user_id = request.query_params.get('user_id', 'default')
        return JSONResponse(await dashboard.get_user_facts(user_id))

    async def api_agent_facts(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        user_id = request.query_params.get('user_id', 'default')
        return JSONResponse(await dashboard.get_agent_facts(user_id))

    async def api_user_episodes(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        user_id = request.query_params.get('user_id', 'default')
        return JSONResponse(await dashboard.get_user_episodes(user_id))

    async def api_agent_episodes(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        user_id = request.query_params.get('user_id', 'default')
        return JSONResponse(await dashboard.get_agent_episodes(user_id))

    async def api_audit(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        return JSONResponse(await dashboard.get_audit())

    async def metrics_endpoint(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        return PlainTextResponse(m.render_prometheus())

    async def metrics_json(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        return JSONResponse(m.render_json())

    async def auth_keys(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        from features.auth import api_key_auth
        return JSONResponse(api_key_auth.list_keys())

    async def auth_create(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        from features.auth import api_key_auth
        body = await request.json()
        key = api_key_auth.create_key(body.get('user_id', 'default'), body.get('label', ''))
        return JSONResponse({'api_key': key})

    async def backup_trigger(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        from features.backup_cron import backup_cron
        path = backup_cron.backup_now()
        return JSONResponse({'path': path})

    async def backup_list(request):
        if not check_auth(request):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
        if not await check_rate_limit(request):
            return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
        from features.backup_cron import backup_cron
        return JSONResponse(backup_cron.list_backups())

    async def health_endpoint(request):
        import time as _time
        from shared.connection import connection_manager
        start = _time.time()
        try:
            conn = await connection_manager.get('memory.db')
            await (await conn.execute('SELECT 1')).fetchone()
            db_ok = True
        except Exception:
            db_ok = False
        db_latency = _time.time() - start
        status = 'ok' if db_ok else 'degraded'
        return JSONResponse({
            'status': status,
            'version': '1.0.0',
            'uptime_seconds': _time.time() - _server_start_time,
            'db': {'connected': db_ok, 'latency_ms': round(db_latency * 1000, 1)},
        })

    async def ready_endpoint(request):
        from shared.migrations import migration_manager
        try:
            current = await migration_manager.get_current_version()
            ready = False
            if isinstance(current, (int, float)):
                ready = current >= 2
        except Exception:
            ready = False
        return JSONResponse({'ready': ready, 'migration_version': current if isinstance(current, (int, float)) else 0})

    async def alive_endpoint(request):
        return JSONResponse({'alive': True})

    app = Starlette(
        routes=[
            Route('/health', health_endpoint),
            Route('/ready', ready_endpoint),
            Route('/alive', alive_endpoint),
            Route('/dashboard', dashboard_page),
            Route('/api/stats', api_stats),
            Route('/api/user/facts', api_user_facts),
            Route('/api/agent/facts', api_agent_facts),
            Route('/api/user/episodes', api_user_episodes),
            Route('/api/agent/episodes', api_agent_episodes),
            Route('/api/audit', api_audit),
            Route('/api/auth/keys', auth_keys),
            Route('/api/auth/create', auth_create, methods=['POST']),
            Route('/api/backup/trigger', backup_trigger, methods=['POST']),
            Route('/api/backup/list', backup_list),
            Route('/metrics', metrics_endpoint),
            Route('/metrics/json', metrics_json),
            Mount('/', app=mcp.streamable_http_app()),
        ],
    )

    from starlette.middleware.base import BaseHTTPMiddleware

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path in ('/mcp', '/health', '/ready', '/alive'):
                return await call_next(request)
            if os.environ.get('MCP_AUTH_DISABLED'):
                return await call_next(request)
            auth = request.headers.get('Authorization', '')
            if auth and not bearer_auth.verify(auth):
                return JSONResponse({'error': 'Invalid token'}, status_code=401)
            return await call_next(request)

    app.add_middleware(AuthMiddleware)
    allowed_origins = config.get('cors', 'allowed_origins', default=['http://localhost:*', 'http://127.0.0.1:*'])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=['GET', 'POST', 'DELETE'],
        expose_headers=['Mcp-Session-Id'],
    )

    import signal
    def _shutdown_handler(signum, frame):
        sig_name = signal.Signals(signum).name
        from features.backup_cron import backup_cron
        from shared.read_only import read_only_replica
        from shared.saga import saga_watchdog
        backup_cron.stop()
        saga_watchdog.stop()
        read_only_replica.stop()
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    main()