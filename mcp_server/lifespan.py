import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from mcp.server.mcpserver import MCPServer
from config import config
from mcp_server.context import AppContext
from shared.read_only import read_only_replica
from features.backup_cron import backup_cron
from lifecycle.importance_scheduler import importance_scheduler


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncGenerator[AppContext, None]:
    from shared.migrations import migration_manager

    result = await migration_manager.migrate()
    logging.getLogger(__name__).info(f"Migrations: {result}")

    await asyncio.to_thread(read_only_replica.sync)

    ctx = AppContext()

    async def _delayed_start() -> None:
        await asyncio.sleep(5)
        backup_cron.start()
        importance_scheduler.start()
        logging.getLogger(__name__).info("Background tasks started after delay")

    asyncio.create_task(_delayed_start())

    async def _periodic_tasks() -> None:
        from lifecycle.consolidation import ConsolidationEngine
        from lifecycle.forgetting import forgetting_system

        last_compaction: float = 0.0
        last_consolidation: float = 0.0
        while True:
            try:
                await asyncio.sleep(900)  # 15 minutes
                await forgetting_system.cleanup()

                # Run compaction every 1 hour
                now = asyncio.get_event_loop().time()
                if now - last_compaction >= 3600:
                    await forgetting_system.run_cleanup(user_id="default")
                    last_compaction = now

                # Consolidation sweep every 1 hour: promote high-weight episodes
                # into L4 per layer. Housekeeping must not depend on an agent
                # calling a tool first.
                if now - last_consolidation >= 3600:
                    min_weight = float(config.get_forgetting("consolidate_weight_threshold") or 0.7)
                    for layer in ("user", "agent"):
                        engine = ConsolidationEngine(layer=layer)
                        promoted = await engine.consolidate_episodes("default", min_weight=min_weight)
                        if promoted:
                            logging.getLogger(__name__).info("Consolidation sweep: %d episodes promoted (layer=%s)", promoted, layer)
                    last_consolidation = now
            except asyncio.CancelledError:
                break
            except Exception:
                logging.getLogger(__name__).exception("Periodic task error")

    periodic_task = asyncio.create_task(_periodic_tasks())
    try:
        yield ctx
    finally:
        periodic_task.cancel()
        with suppress(asyncio.CancelledError):
            await periodic_task
        importance_scheduler.stop()
        backup_cron.stop()
