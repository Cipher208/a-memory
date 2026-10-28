# autohooks/daemon.py
"""Poll loop dispatching new_message events (spec S4).

Triggers-not-timers: the poll is only transport. Saves happen server-side
inside dispatch_event handlers when evaluate_importance crosses the
configured threshold. At-least-once delivery: cursor persists after each
batch; a crash may re-dispatch at most one batch.

THE PROMISE IS ABOUT DELIVERY, NOT ABOUT SURVIVAL, and it used to be broken
in the quietest possible way. The hook registry catches a handler's exception
and reports it as a result, so the daemon never saw an exception at all: it
read "one handler failed" as "the message was handled", advanced the cursor
past the message, and the message was lost for good -- the source is a chat
log that cannot be re-read from the middle.

So the cursor now follows the CLASSIFICATION of the failure:

* a TRANSIENT failure (the registry marks it: a BUSY database, another
  writer holding the lock) means the work was not done. The cursor stays
  behind the message, the next tick offers it again, and the message is
  written on the first attempt that gets through. This is the at-least-once
  promise.
* a PERMANENT failure (a handler bug, a schema that does not match the
  code) means the message can never be delivered. The cursor moves on:
  holding it would stall the whole ingestion on one poison message,
  silently, forever. The failure is logged at ERROR level, because
  nothing else will report it again.

AND A DECLINE THAT IS NOT A VERDICT IS NOT A DELIVERY EITHER. A dispatch
result that carries no handler result at all -- the dedup middleware declines
with `{"status": "deduped"}` -- means the event never reached a handler.
Reading that as a delivery advanced the cursor past a message that had not
been written. The cursor stays behind such a message, the message is offered
again on the next tick, and the decline is logged so the stall is visible
instead of silent.

WHAT THE LIVE EVIDENCE SAYS, because the earlier narrative in this file was
wrong: the component that LOSES the write lock on the house base is the MCP
SERVER's importance sweep, not this daemon -- both autohooks units report
`NRestarts=0`. That is what the architecture points at, NOT an observation:
the journals of the user units hold no `database is locked` today (the last
ones are from 18.10, and they are the adaptive-threshold counter write, not
the delivery path). The daemon's real failure was never dying. It was LOSING
the message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sqlite3
import time
from typing import TYPE_CHECKING, Any

# Liveness-sweep gate: run the central dangling-edge prune at most this often
# (seconds) from the daemon loop. See lifecycle.graph_sanitation.prune_dangling_edges.
_EDGE_PRUNE_SECONDS = 600.0

# Connection-health gate (seconds). The daemon holds descriptors on the memory base
# that the async manager does not know about; this is how that number is watched.
# Measured 16.10.2026: 41 descriptors on one file while running only 9 threads, where
# an identical daemon for another base held 1. A manager-only reading cannot see that
# difference, so the reading counts /proc/self/fd as well.
_HEALTH_LOG_SECONDS = 600.0

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from autohooks.config import AgentConfig
    from autohooks.source import Batch, SqliteSource

logger = logging.getLogger("autohooks.daemon")

if TYPE_CHECKING:
    Dispatch = Callable[..., Awaitable[dict[str, Any]]]
else:
    Dispatch = Any


def rate_limited(result: Any) -> bool:
    """Report whether the pipeline declined this event on the rate limit.

    The distinction matters because this is the only decline that means "not now"
    rather than "no". An unimportant or duplicate event should be dropped and the
    cursor should move on; a rate-limited one must not be, because the source is a
    chat log the daemon can never re-read from the middle — advancing past it
    loses the message for good.

    This was not hypothetical. A replay of a live persona's history hit the
    limiter 2590 times and lost 63 of 142 substantive messages, each leaving no
    row in the journal, no row in the dispatch log, and no warning the operator
    would see.
    """
    from shared.middleware import RATE_LIMIT_BLOCK_PREFIX

    return isinstance(result, dict) and result.get("skipped") is True and RATE_LIMIT_BLOCK_PREFIX in str(result.get("reason", ""))


def transient_failure(result: Any) -> str | None:
    """Return the error text of the first TRANSIENT failure in a dispatch result.

    The hook registry classifies a failed handler and says so in the result
    (`{"error": ..., "transient": True}`), because it catches the exception
    and the caller never sees one. A transient failure means the work was
    NOT done: the caller must not treat the event as handled, or it will
    advance the cursor past a message that was never written.

    None means "no transient failure in this result": the dispatch
    succeeded, it declined the event (a rate limit, an unimportant
    message), or it failed permanently (a handler bug, a schema that does
    not match the code).

    A result that is not a registry result at all is a different thing and
    is asked about separately: `not_delivered` reports an event that never
    reached a handler.
    """
    if not isinstance(result, dict):
        return None
    results = result.get("results")
    if not isinstance(results, list):
        return None
    for entry in results:
        if isinstance(entry, dict) and entry.get("transient") is True:
            return str(entry.get("error", "unknown error"))
    return None


def _busy(exc: BaseException) -> bool:
    """Report whether a database failure is transient (another writer holds the lock).

    The boundary itself lives in `shared.sqlite_busy`: the registry asks the
    same question when it classifies a handler's failure, and two copies of
    it are two answers waiting to diverge. The import is inside the function
    because importing `shared` at module level resolves the data directory at
    import time, and this module is imported before the environment is set.
    """
    from shared.sqlite_busy import is_busy

    return is_busy(exc)


def not_delivered(result: Any) -> str | None:
    """Return why the event never reached a handler, or None.

    Three shapes reach the daemon, and only one of them means "written":

    * a REGISTRY RESULT carries a `results` list -- the handlers ran. The
      daemon reads the failure classification out of it.
    * a VERDICT carries `skipped` -- the pipeline or the registry refused the
      event on its merits (the hook is disabled, there is no handler for this
      layer, the event was unimportant). The message has been dealt with and
      the cursor may move on.
    * anything else is a DECLINE WITH NO VERDICT. The event was handed to the
      middleware and dropped on the way to the handler, so nothing was
      written. The shape that matters is `{"status": "deduped"}` from the
      dedup middleware: it is not a verdict on the EVENT (the same text is
      welcome again), it is a verdict on the MOMENT.

    Reading the third shape as a delivery lost the message: the daemon
    advanced the cursor past a message that had not been written, and the
    source is a chat log that cannot be re-read from the middle.
    """
    if not isinstance(result, dict):
        return "the dispatch returned no result at all"
    if isinstance(result.get("results"), list):
        return None
    if result.get("skipped") is True:
        return None
    return "the event was declined before it reached a handler"


def load_cursor(state_file: Path) -> int | None:
    if not state_file.exists():
        return None
    return int(json.loads(state_file.read_text(encoding="utf-8"))["cursor"])


def save_cursor(state_file: Path, cursor: int) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")


def source_went_backwards(source_max_id: int, cursor: int) -> bool:
    """Report whether the source's newest row is now BEHIND the saved cursor.

    A source is a chat database, and a chat database can be renumbered under the
    daemon: cowagent's `migrate_conversations_to_global` (via
    `_merge_secondary_agents` -> `_merge_one_agent`) folds each secondary agent's
    `index.db` into the default agent's file and re-issues every message id on the
    way in (`id -> NULL so the global file re-issues AUTOINCREMENT ids`). After
    that, the ids a cursor was counting no longer describe the rows it had
    consumed.

    THREE THINGS THIS IS AND IS NOT, because the first version of this docstring
    overstated all three.

    * It is PREVENTIVE. No live occurrence has been observed. The case that
      prompted it — one base whose daemon looked healthy and dispatched nothing —
      turned out to be a different phenomenon: that platform stopped writing rows
      on 29.09, so the source simply stopped growing. Measured at the time,
      `max(id)` was 8709 and the saved cursor was 8709: EQUAL, not behind. This
      check would not have fired. A stalled source and a renumbered one look alike
      from the cursor and are not the same thing.
    * Only the MERGE re-issues ids. The multi-agent schema rebuild recreates
      `sessions`/`messages` under composite `(agent_id, session_id)` keys, but it
      copies the id explicitly (`INSERT INTO messages_new (id, agent_id, ...)
      SELECT id, agent_id, ... FROM messages`) — it preserves them. An earlier
      version of this note claimed the rebuild also renumbered, which is wrong and
      would send a reader hunting the wrong mechanism.
    * It is cheap and safe to keep anyway. Should a merge ever land on a base this
      daemon tails, `WHERE id > cursor` would select nothing and stay silent
      forever, which is unrecoverable without this; erroring the other way costs
      one slow re-read.

    The cursor is only ever compared with `max_id()` on FIRST start, when no
    state file exists. Once a cursor is saved, nothing notices the source moving
    underneath it. This is the check that notices.

    Answers False whenever the two are not both integers. `cursor_column` is
    whatever the config names, so a source may legitimately declare a TEXT key
    (here every live source uses an integer one, but nothing enforces that).
    `SqliteSource` orders such a key as text, and `'msg_abc' < 64763` raises
    TypeError inside the poll loop — turning a timing problem into a dead daemon.
    Declining to rewind when the ordering cannot be established costs a missed
    reset; raising costs the daemon.
    """
    if not isinstance(source_max_id, int) or not isinstance(cursor, int):
        return False
    return source_max_id < cursor


async def run_daemon(
    cfg: AgentConfig,
    source: SqliteSource,
    mem: Any,
    graph: Any,
    rag: Any,
    *,
    max_iterations: int | None = None,
    poll: Callable[[float], Awaitable[None]] | None = None,
    dispatch: Dispatch | None = None,
    resolve: Callable[[str], tuple[Any, Any, Any]] | None = None,
) -> None:
    """Run the poll loop until SIGTERM/SIGINT (or max_iterations for tests).

    resolve: layer -> (mem, graph, rag) for cross-layer dispatch (S10 speaker
    axis); None disables the switch (unit tests / single-layer clients).
    """
    from autohooks.config import dispatch_layer
    from hooks.external import dispatch_event

    dispatch = dispatch or dispatch_event
    layer_mems: dict[str, tuple[Any, Any, Any]] = {}
    try:
        saved = load_cursor(cfg.state_file)
        # A daemon that has consumed nothing has no cursor yet, and the
        # baseline (the newest existing row) is read on the first tick that
        # can read it at all. A busy source must not make that baseline
        # permanent, so the read is retried rather than assumed.
        need_baseline = saved is None
        cursor: int = saved if saved is not None else 0

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        iterations = 0
        last_pressure = 0
        # None sentinel, not 0.0: time.monotonic() is CLOCK_MONOTONIC (time since
        # boot), so a 0.0 baseline made the first sweep fire only when the host
        # had been up ≥10 min — it passed on long-lived VPS, failed on fresh CI.
        last_prune: float | None = None
        last_health: float | None = None

        async def _next_tick() -> bool:
            """Advance the counter, wait for the next tick; False means stop.

            The counter lives here so that a tick which was SKIPPED still
            counts and still waits. The old inline tail was the only place
            that advanced the counter, so a skipped tick would have spun.
            """
            nonlocal iterations
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return False
            if poll is not None:
                await poll(cfg.poll_seconds)
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=cfg.poll_seconds)
            return True

        while not stop.is_set():
            # A source can be replaced or renumbered under a running daemon (see
            # `source_went_backwards`). Left unnoticed, `WHERE id > cursor` then
            # selects nothing on every poll and the daemon looks healthy while
            # dispatching nothing. No live occurrence has been observed — this is
            # a preventive check, kept because that failure is unrecoverable
            # without it. The stalled base that first drew attention here stopped
            # growing for its own reason, and its cursor equalled its max id, so
            # this branch would not have fired.
            #
            # The rewind goes to ZERO, not to the new maximum. Resetting to the
            # maximum would be a no-op dressed as a repair: it would skip
            # everything the renumbered source now holds, which is precisely the
            # history that has to be re-read. Once ids no longer map to what was
            # consumed, no id is trustworthy, so the only safe target is the
            # beginning.
            #
            # This is affordable because L0 dedups by content hash: messages
            # already captured collapse to duplicates and nothing is stored
            # twice. Re-reading a whole source is slow; never reading it again is
            # permanent. The event is logged loudly, because a silent rewind would
            # hide the merge that caused it.
            #
            # The SOURCE is a chat database written by ANOTHER process, so
            # `database is locked` is the one error this daemon can plausibly
            # die of -- not from its own writes, but from a read that lost the
            # race with the writer that owns the file. It is contained here:
            # the tick is skipped, the reason is reported, and the next tick
            # reads again. A permanent database error (a wrong column, a
            # missing table) is reported at ERROR level instead -- it is a
            # broken setup, not a busy moment, but the daemon has no cursor
            # to hold here and killing it would only produce a restart loop.
            batch: Batch | None = None
            try:
                if need_baseline:
                    # First-run baseline: newest existing row, no history replay (S4).
                    cursor = source.max_id()
                    save_cursor(cfg.state_file, cursor)
                    need_baseline = False
                else:
                    source_max = source.max_id()
                    if source_went_backwards(source_max, cursor):
                        logger.warning(
                            "source went backwards: max id %s < cursor %s — the source was "
                            "replaced or renumbered (a multi-agent merge re-issues message ids). "
                            "Resetting the cursor to 0 and re-reading the whole source; L0 dedups "
                            "by content hash, so this is slow but lossless.",
                            source_max,
                            cursor,
                        )
                        cursor = 0
                        save_cursor(cfg.state_file, cursor)
                batch = source.fetch_after(cursor, cfg.batch_limit)
            except sqlite3.Error as exc:
                if _busy(exc):
                    logger.warning(
                        "source read failed (%s: %s) — the source database is busy or unreadable; skipping this tick and reading again on the next one",
                        type(exc).__name__,
                        exc,
                    )
                else:
                    # A permanent database failure in the SOURCE: a missing
                    # table, a wrong column. There is no message to hold a
                    # cursor for here, and killing the daemon would only
                    # produce a restart loop, so the tick is skipped and the
                    # operator gets the truth on every tick.
                    detail = str(exc)
                    logger.exception(
                        "source read failed with a PERMANENT database error (%s: %s) — the source database is broken; skipping this tick and reporting it on every tick",
                        type(exc).__name__,
                        detail,
                    )
                batch = None

            if batch is None:
                if not await _next_tick():
                    return
                continue

            # The cursor advances only past messages that were really handled.
            # `dispatch` can decline one, and the rate limit in particular means
            # "not now": the source is a chat log that cannot be re-read from the
            # middle, so advancing past a declined message loses it permanently.
            # Holding the cursor keeps the at-least-once promise this module
            # documents in its docstring — which the old `cursor = batch.cursor`
            # quietly broke for exactly the messages that most needed delivering.
            handled_upto = cursor
            for msg in batch.messages:
                # Empty text can never become a memory — `_new_message` already
                # answers `no_text_or_mem` for it — but it consumed a rate-limit
                # slot on the way there, because the middleware pipeline runs
                # ahead of the handler. A chat database that stores one row per
                # streamed chunk produces thousands of these (2034 empty
                # assistant rows in one live log), and they crowded the real
                # messages out of the budget. Skipping them here costs nothing
                # and removes the flood at its source.
                if not (msg.text or "").strip():
                    handled_upto = msg.source_id
                    continue
                # S10: role from the source row; persona_owner assistant
                # messages switch to the agent layer (cached per layer).
                role = msg.sender or ""
                d_layer = dispatch_layer(cfg, {"role": role})
                mem_d, graph_d, rag_d = mem, graph, rag
                if d_layer != cfg.layer:
                    if resolve is None:
                        d_layer = cfg.layer
                    else:
                        if d_layer not in layer_mems:
                            layer_mems[d_layer] = resolve(d_layer)
                        mem_d, graph_d, rag_d = layer_mems[d_layer]
                # F96: the Hermes plugin pushes the same source message and
                # usually wins the race, without a `source_msg_id`. Dispatching
                # into a `duplicate_l0_block` refusal only writes a rejection
                # and loses provenance. Find the row that already holds this
                # block, stamp the id it lacks, and move the cursor on. The
                # duplicate path never reaches the dispatcher's own
                # `find_block`; a NEW message falls through to the normal
                # dispatch.
                from shared.l0 import find_block, stamp_source_msg_id

                prior = await find_block(d_layer, cfg.user_id, msg.text)
                if prior is not None:
                    stamped = await stamp_source_msg_id(prior, msg.source_id)
                    logger.debug(
                        "source_msg_id=%s already captured as l0 id %s (stamped=%s) — skipping dispatch",
                        msg.source_id,
                        prior,
                        stamped,
                    )
                    handled_upto = msg.source_id
                    continue
                try:
                    result = await dispatch(
                        "new_message",
                        d_layer,
                        cfg.user_id,
                        {
                            "text": msg.text,
                            "sender": msg.sender,
                            "role": role,
                            "persona_owner": cfg.persona_owner,
                            "ts": msg.ts,
                            "source_msg_id": msg.source_id,
                        },
                        mem_d,
                        graph_d,
                        rag_d,
                    )
                except sqlite3.Error as exc:
                    # Only DATABASE failures are contained here, and only
                    # because that one failure mode is a fact of life: a busy
                    # database clears by itself, once per busy_timeout
                    # window, while another writer holds the lock.
                    #
                    # NOTE what this catch is NOT. The handler's own
                    # exception does not arrive here: `hooks/registry.py`
                    # catches it and reports it as a result. This branch
                    # only sees a database error raised OUTSIDE a handler --
                    # in the middleware pipeline, in the dispatch log, in
                    # the rejection log. The handler's failure is classified
                    # by the registry and handled by the branch below.
                    #
                    # Everything else PROPAGATES. A `ValueError` for an unknown
                    # event type, a `TypeError` from a wrong call shape -- those
                    # are programming errors, and a daemon that contained them
                    # would hold the cursor behind a message it can never
                    # deliver and poll on it forever, silently.
                    #
                    # A BUSY database is transient: the message is NOT handled,
                    # so the cursor stays behind it and the next tick tries it
                    # again. That is the at-least-once promise this module's
                    # docstring makes, and it is why the batch stops here
                    # instead of skipping to the next message: skipping would
                    # move the cursor past a message that was never delivered.
                    if _busy(exc):
                        logger.warning(
                            "dispatch failed at source_msg_id=%s (layer=%s): %s: %s — holding the cursor at %s and retrying",
                            msg.source_id,
                            d_layer,
                            type(exc).__name__,
                            exc,
                            handled_upto,
                        )
                        break
                    # A PERMANENT database failure: a missing table, a wrong
                    # column, a broken constraint. It never clears, so holding
                    # the cursor would stall the whole ingestion on one
                    # undeliverable message. The message is treated as
                    # handled, the cursor moves on, and the failure is logged
                    # at ERROR level -- loudly, because nothing else will
                    # report it again.
                    detail = str(exc)
                    logger.exception(
                        "dispatch failed with a PERMANENT database error at source_msg_id=%s (layer=%s): %s: %s — the message cannot be delivered; treating it as handled and moving the cursor on",
                        msg.source_id,
                        d_layer,
                        type(exc).__name__,
                        detail,
                    )
                    handled_upto = msg.source_id
                    continue
                failure = transient_failure(result)
                if failure is not None:
                    # A TRANSIENT failure: the handler raised a database error
                    # and the work was NOT done. The registry swallowed the
                    # exception, so this result is the only trace of it -- and
                    # before this rule the daemon read the result as a
                    # delivery, moved the cursor past the message, and lost it
                    # for good. The cursor stays where it is and the next tick
                    # offers the same message again.
                    logger.warning(
                        "transient delivery failure at source_msg_id=%s (layer=%s): %s — holding the cursor at %s and retrying",
                        msg.source_id,
                        d_layer,
                        failure,
                        handled_upto,
                    )
                    break
                if rate_limited(result):
                    # Retry after the poll interval; the limiter's window is a
                    # minute, so the same message goes through on a later pass.
                    logger.warning(
                        "rate limited at source_msg_id=%s (layer=%s) — holding the cursor at %s and retrying",
                        msg.source_id,
                        d_layer,
                        handled_upto,
                    )
                    break
                undelivered = not_delivered(result)
                if undelivered is not None:
                    # The event never reached a handler: a middleware declined
                    # it on the way there. Nothing was written, so the cursor
                    # stays behind the message and the next tick offers it
                    # again. The warning is what makes the stall VISIBLE --
                    # a silent decline is how the message was lost.
                    logger.warning(
                        "dispatch at source_msg_id=%s (layer=%s) never reached a handler (%s) — holding the cursor at %s and retrying",
                        msg.source_id,
                        d_layer,
                        undelivered,
                        handled_upto,
                    )
                    break
                handled_upto = msg.source_id
                logger.debug("dispatched msg %s (layer=%s): %s", msg.source_id, d_layer, result)
            if handled_upto != cursor:
                cursor = handled_upto
                save_cursor(cfg.state_file, cursor)
            # E16: memory_pressure — ariel-side emitter (L1 ring growth with hysteresis).
            size = len(mem.l1.get_full()) if mem is not None and hasattr(mem, "l1") and hasattr(mem.l1, "get_full") else 0
            if size > 40 and size - last_pressure >= 10:
                # The pressure event is a write on the same base as everything
                # else, so a busy database here is the same failure as
                # anywhere else: it is reported and skipped, and the event is
                # retried on a later tick. A programming error still
                # propagates -- it is a broken setup, not a busy moment.
                try:
                    await dispatch("memory_pressure", cfg.layer, cfg.user_id, {"l1_size": size}, mem, graph, rag)
                except sqlite3.Error as exc:
                    if _busy(exc):
                        logger.warning(
                            "memory_pressure dispatch failed (%s: %s) — skipping the event and retrying on the next tick",
                            type(exc).__name__,
                            exc,
                        )
                    else:
                        detail = str(exc)
                        logger.exception(
                            "memory_pressure dispatch failed with a PERMANENT database error (%s: %s) — skipping the event",
                            type(exc).__name__,
                            detail,
                        )
                else:
                    last_pressure = size
            # 2026-09-16: central liveness sweep — a frozen read-snapshot writer
            # (this daemon included) can recreate edges toward purged nodes;
            # housekeeping keeps dangling rows at dust scale. Failures must not
            # break the poll loop.
            now_mono = time.monotonic()
            if graph is not None and (last_prune is None or now_mono - last_prune >= _EDGE_PRUNE_SECONDS):
                last_prune = now_mono
                with contextlib.suppress(Exception):
                    from lifecycle.graph_sanitation import prune_dangling_edges

                    pruned = await prune_dangling_edges(graph._cm)
                    if pruned:
                        logger.info("dangling-edge sweep: pruned %d", pruned)

            # Connection health. The daemon runs on its own base and opens
            # synchronous handles the async manager never sees, so a reading built
            # only from manager bookkeeping would report one connection while the
            # process holds dozens of descriptors — the exact shape that hid this
            # for two days. Best-effort: a diagnostic must not stop the poll loop.
            if last_health is None or now_mono - last_health >= _HEALTH_LOG_SECONDS:
                last_health = now_mono
                with contextlib.suppress(Exception):
                    from shared.connection import connection_health

                    health = await connection_health()
                    logger.info(
                        "connection health: connections=%s leaked_workers=%s open_db_fds=%s unmanaged_fds=%s",
                        health["connections"],
                        health["leaked_workers"],
                        health["open_db_fds"],
                        health["unmanaged_fds"],
                    )
            if not await _next_tick():
                return
    finally:
        source.close()
