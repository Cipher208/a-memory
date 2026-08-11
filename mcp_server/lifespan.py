import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from mcp.server.fastmcp import FastMCP
from mcp_server.context import AppContext
from shared.read_only import read_only_replica
from features.backup_cron import backup_cron
from lifecycle.importance_scheduler import importance_scheduler

@asynccontextmanager
async def lifespan(server: FastMCP):
    from shared.migrations import migration_manager

    result = await migration_manager.migrate()
    logging.getLogger(__name__).info(f"Migrations: {result}")

    await asyncio.to_thread(read_only_replica.sync)

    ctx = AppContext()

    async def _delayed_start():
        await asyncio.sleep(5)
        backup_cron.start()
        importance_scheduler.start()
        logging.getLogger(__name__).info("Background tasks started after delay")

    asyncio.create_task(_delayed_start())

    async def _periodic_tasks():
        from lifecycle.forgetting import ForgettingSystem
        from lifecycle.compactor import memory_compactor

        forgetting = ForgettingSystem()
        last_compaction = 0
        while True:
            try:
                await asyncio.sleep(900)  # 15 minutes
                await forgetting.cleanup()

                # Run compaction every 1 hour
                now = asyncio.get_event_loop().time()
                if now - last_compaction >= 3600:
                    await memory_compactor.run_cleanup(user_id="default")
                    last_compaction = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.getLogger(__name__).exception("Periodic task error: %s", e)

    periodic_task = asyncio.create_task(_periodic_tasks())
    try:
        yield ctx
    finally:
        periodic_task.cancel()
        with suppress(asyncio.CancelledError):
            await periodic_task
        importance_scheduler.stop()
        backup_cron.stop()
