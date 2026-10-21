import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from mcp.server.mcpserver import MCPServer
from config import config
from mcp_server.context import AppContext
from shared.read_only import read_only_replica
from features.backup_cron import backup_cron
from lifecycle.importance_scheduler import importance_scheduler


async def _connection_health() -> dict[str, Any]:
    """Report this process's connection health. Never raises.

    Delegates to `shared.connection.connection_health`, which counts the manager's own
    bookkeeping and the process's real descriptor table separately. Both are needed:
    the manager cannot see synchronous `with sqlite3.connect(...)` handles, and a
    reading built only from it would call a process holding 41 open files
    `connections=1 leaked_workers=0`.

    Kept as a name here because the maintenance loop's tests import it from this
    module; the logic lives in shared/ so the autohooks daemon can use the same one.
    """
    from shared.connection import connection_health

    health = await connection_health()
    logging.getLogger(__name__).info(
        "connection health: connections=%s dbs=%s leaked_workers=%s open_db_fds=%s unmanaged_fds=%s",
        health["connections"],
        ",".join(health["dbs"]) or "-",
        health["leaked_workers"],
        health["open_db_fds"],
        health["unmanaged_fds"],
    )
    return health


async def _storage_pass() -> None:
    """Size monitoring, auto-VACUUM and the connection-health reading.

    Extracted from the periodic loop so the wiring is testable: `_connection_health`
    is useful only if the loop actually calls it, and a test that calls it directly
    cannot tell the difference between "wired" and "written and forgotten". The first
    mutation run proved that — deleting the call from the loop left every test green.

    Both halves are best-effort by construction: this runs inside the maintenance
    loop, and an exception here would cost the whole pass.
    """
    log = logging.getLogger(__name__)

    # DB size monitoring + auto-VACUUM.
    try:
        from features.db_maintenance import run_db_maintenance

        reports = await run_db_maintenance()
        for r in reports:
            if r.vacuumed_mb:
                log.info("DB maintenance: %s vacuumed %.1f MB -> %.1f MB", r.name, r.vacuumed_mb, r.size_mb)
    except Exception:
        log.exception("DB maintenance failed")

    # Connection health, read from INSIDE the process.
    #
    # Every diagnosis of the 15–16.10 lock storms had to be made from outside, poking
    # at /proc: descriptor counts that oscillated between 49 and 57 and could not be
    # read as a trend, and no way to ask the process how many connections it believed
    # it had. The numbers that answer that have existed all along — `stats()` and
    # `leaked_connection_workers()` — and nothing called them in production.
    #
    # Logged in the same pass the failures come from, so the next storm has a reading
    # taken moments before it rather than after the fact.
    await _connection_health()


# How long startup waits its turn on the maintenance turnstile before running
# migrations ungated (spec S3). Bounded: the normal wait is milliseconds —
# only a genuinely long migration makes a follower wait at all.
MIGRATION_GATE_TIMEOUT_S: float = 120.0


async def run_startup_gated(
    migrate: Callable[[], Awaitable[Any]],
    sync: Callable[[], Any],
    *,
    base_dir: str | Path | None = None,
    gate_timeout: float = MIGRATION_GATE_TIMEOUT_S,
) -> None:
    """Run migrations and the replica sync under the maintenance turnstile.

    Every instance passes through here before serving, so exactly one migration
    writer goes at a time and the rest wait for their turn instead of failing
    the client's connect with `CancelledError` (spec S1/S3 — the 07:02/08:02
    shape). `migrate()` is idempotent at head (see
    `tests/test_shared/test_migrate_skips_when_at_head.py`), so a follower that
    gets the turn right after the leader observes a no-op.

    On timeout the gate is abandoned and both writes run ungated: better a
    noisy success than a server that never comes up. Leadership (who runs the
    background set) is decided separately, in `lifespan()` — followers must
    pass this gate too, they serve tools against the same schema.
    """
    from shared import maintenance_lock

    log = logging.getLogger(__name__)
    # In a thread: acquire() polls with a synchronous time.sleep, and a
    # follower may wait out a long migration — the event loop must keep
    # servicing other tasks (and signal delivery) meanwhile. A cancellation
    # landing while the thread still polls leaves the fd to this process;
    # fcntl drops it with the process, which is what startup cancellation
    # means at that point.
    holder = await asyncio.to_thread(maintenance_lock.acquire, base_dir, gate_timeout)
    if holder is None:
        log.warning(
            "migration gate timed out after %.1fs; running migrations ungated",
            gate_timeout,
        )
    try:
        result = await migrate()
        log.info("Migrations: %s", result)
        await asyncio.to_thread(sync)
    finally:
        maintenance_lock.release(holder)


# How often a follower re-attempts leadership (spec S4). Bounded failover:
# when a leader's process dies its flock dies with it, so the first retry
# after that moment wins — worst case one cadence of no housekeeping.
PROMOTION_RETRY_S: float = 60.0


async def leadership_loop(
    claim: Callable[[], int | None],
    on_claim: Callable[[int], None],
    on_promote: Callable[[], None],
    stop: asyncio.Event,
    *,
    retry_s: float = PROMOTION_RETRY_S,
) -> None:
    """Become the leader and stay one until `stop` (spec S4).

    A lost claim is the normal state of a follower, not an error: retry at a
    slow cadence instead of spinning. The fd is handed to `on_claim` before
    `on_promote` so the caller can always release what leadership holds — the
    loop itself never releases, because cancellation between claim and wait
    must not race two releasers (lifespan's `finally` owns the fd).
    """
    while not stop.is_set():
        try:
            fd = claim()
        except OSError:
            logging.getLogger(__name__).warning("leadership claim failed; retrying in %.0fs", retry_s, exc_info=True)
            fd = None
        if fd is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=retry_s)
            except TimeoutError:
                continue
            return
        on_claim(fd)
        on_promote()
        await stop.wait()
        return


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

        from shared import maintenance_lock
        from shared.connection import connection_manager

        last_compaction: float = 0.0
        last_consolidation: float = 0.0
        while True:
            holder = None
            try:
                await asyncio.sleep(900)  # 15 minutes

                # One writer per base. Every process serving this base used to
                # run this loop, and they all wrote the same SQLite file:
                # `Decay failed` with `database is locked` went 2-4/day in
                # September -> 14 -> 65 -> 34, and the WAL reached 35 MB without
                # ever checkpointing (measured 14.10.2026). The scheduler holds
                # the same lock, so the two kinds of housekeeping take turns
                # instead of colliding, and a process that cannot claim it
                # simply waits for its next turn.
                holder = maintenance_lock.acquire(connection_manager.base_dir)
                if holder is None:
                    logging.getLogger(__name__).debug("periodic housekeeping skipped: not the maintenance leader")
                    continue

                await forgetting_system.cleanup()

                # 2026-09-16: central liveness sweep (see autohooks/daemon.py) —
                # dangling edges from stale-snapshot writers are dust, not debt.
                from lifecycle.graph_sanitation import prune_dangling_edges
                from shared.connection import connection_manager

                pruned = await prune_dangling_edges(connection_manager)
                if pruned:
                    logging.getLogger(__name__).info("dangling-edge sweep: pruned %d", pruned)

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
                        await buf.ensure()

                        conn = await buf._cm.get("memory.db")
                        cur = await conn.execute("SELECT DISTINCT user_id FROM staging_memories WHERE layer=?", (layer,))
                        users = {r[0] for r in await cur.fetchall()}
                        staged_any = False
                        for uid in sorted(users):
                            items = await buf.get_staging(uid)
                            if not items:
                                continue
                            from config import config as _cfg

                            if _cfg.get("staging", "enabled", default=True):
                                from lifecycle.consolidation import stage_consolidation

                                pid = await stage_consolidation(uid, layer, items, min_importance=min_weight)
                                res: dict[str, Any] = {"staged": pid is not None}
                            else:
                                res = dict(await engine.consolidate_staging(uid, items, min_importance=min_weight))
                            await buf.clear_staging(uid)
                            staged_any = True
                            logging.getLogger(__name__).info("Staging drained (layer=%s user=%s): %s", layer, uid, res)

                        # ponytail: consolidate_episodes not staged (C1.11) — its candidate set
                        # is scan-derived; staging would duplicate the scan. Revisit if a
                        # real gap shows up in proposals review.
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

                    # S6: staging housekeeping — auto-review lane for pattern
                    # sources (the manual queue rots unread: 225 expired on
                    # hermes) + graveyard purge of decided rows past the window.
                    from features.staging import auto_apply_pending, expire_stale, purge_decided_past_window

                    await expire_stale()
                    n_auto = await auto_apply_pending(ctx)
                    n_purged = await purge_decided_past_window()
                    if n_auto or n_purged:
                        logging.getLogger(__name__).info("Staging housekeeping: auto_applied=%d purged=%d", n_auto, n_purged)

                await _storage_pass()
            except asyncio.CancelledError:
                break
            except Exception:
                logging.getLogger(__name__).exception("Periodic task error")
            finally:
                # Hand the base back whatever happened: a leaked lock would
                # silently stop housekeeping for every other process.
                maintenance_lock.release(holder)

    periodic_task = asyncio.create_task(_periodic_tasks())
    try:
        yield ctx
    finally:
        periodic_task.cancel()
        with suppress(asyncio.CancelledError):
            await periodic_task
        importance_scheduler.stop()
        backup_cron.stop()
