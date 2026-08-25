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
        backup_cron.capture_main_loop()
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

                # Consolidation sweep every 1 hour: drain dream staging, then
                # promote high-weight episodes into L4 — per layer. Housekeeping
                # must not depend on an agent calling a tool first.
                if now - last_consolidation >= 3600:
                    from shared.dream_buffer import DreamBuffer

                    min_weight = float(config.get_forgetting("consolidate_weight_threshold") or 0.7)
                    for layer in ("user", "agent"):
                        engine = ConsolidationEngine(layer=layer)
                        buf = DreamBuffer(layer=layer)
                        await buf._init_db()

                        conn = await buf._cm.get("memory.db")
                        cur = await conn.execute("SELECT DISTINCT user_id FROM staging_memories WHERE layer=?", (layer,))
                        users = {r[0] for r in await cur.fetchall()}
                        staged_any = False
                        for uid in sorted(users):
                            items = await buf.get_staging(uid)
                            if not items:
                                continue
                            res = await engine.consolidate_staging(uid, items, min_importance=min_weight)
                            await buf.clear_staging(uid)
                            staged_any = True
                            logging.getLogger(__name__).info("Staging drained (layer=%s user=%s): %s", layer, uid, res)

                        promoted = await engine.consolidate_episodes("default", min_weight=min_weight)

                        await buf.cleanup_old()  # unpromoted staging >24h is ephemeral

                        if promoted or staged_any:
                            logging.getLogger(__name__).info(
                                "Consolidation sweep done (layer=%s): episodes_promoted=%d staged_drained=%s",
                                layer,
                                promoted,
                                staged_any,
                            )
                    last_consolidation = now

                # DB size monitoring + auto-VACUUM (same hourly cadence)
                try:
                    from features.db_maintenance import run_db_maintenance

                    reports = await run_db_maintenance()
                    for r in reports:
                        if r.vacuumed_mb:
                            logging.getLogger(__name__).info("DB maintenance: %s vacuumed %.1f MB -> %.1f MB", r.name, r.vacuumed_mb, r.size_mb)
                except Exception:
                    logging.getLogger(__name__).exception("DB maintenance failed")
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
