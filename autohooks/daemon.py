# autohooks/daemon.py
"""Poll loop dispatching new_message events (spec S4).

Triggers-not-timers: the poll is only transport. Saves happen server-side
inside dispatch_event handlers when evaluate_importance crosses the
configured threshold. At-least-once delivery: cursor persists after each
batch; a crash may re-dispatch at most one batch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
from typing import TYPE_CHECKING, Any

# Liveness-sweep gate: run the central dangling-edge prune at most this often
# (seconds) from the daemon loop. See lifecycle.graph_sanitation.prune_dangling_edges.
_EDGE_PRUNE_SECONDS = 600.0

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from autohooks.config import AgentConfig
    from autohooks.source import SqliteSource

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
    daemon: cowagent's `merge_conversations_to_global` folds each secondary
    agent's `index.db` into the default agent's file and re-issues every message
    id on the way in (`id -> NULL so the global file re-issues AUTOINCREMENT
    ids`), and its multi-agent schema rebuild recreates the messages table
    outright. After that, the ids a cursor was counting no longer describe the
    rows it had consumed.

    The cursor is only ever compared with `max_id()` on FIRST start, when no
    state file exists. Once a cursor is saved, nothing notices the source moving
    underneath it, and `WHERE id > cursor` then selects nothing forever. This is
    the check that notices.

    Answers False whenever the two are not both integers. `cursor_column` is
    whatever the config names, so a source may legitimately declare a TEXT key
    (Ksal's `role`/`text` plumbing shows how loose the mapping is; here it is
    `rowid`, but nothing enforces integer). `SqliteSource` orders such a key as
    text, and `'msg_abc' < 64763` raises TypeError inside the poll loop — turning
    a timing problem into a dead daemon. Declining to rewind when the ordering
    cannot be established costs a missed reset; raising costs the daemon.
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
        cursor = load_cursor(cfg.state_file)
        if cursor is None:
            # First-run baseline: newest existing row, no history replay (S4).
            cursor = source.max_id()
            save_cursor(cfg.state_file, cursor)

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
        while not stop.is_set():
            # A source can be replaced or renumbered under a running daemon (see
            # `source_went_backwards`). Left unnoticed, `WHERE id > cursor` then
            # selects nothing on every poll and the daemon looks healthy while
            # dispatching nothing — the failure one live base sat in for a week.
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
                handled_upto = msg.source_id
                logger.debug("dispatched msg %s (layer=%s): %s", msg.source_id, d_layer, result)
            if handled_upto != cursor:
                cursor = handled_upto
                save_cursor(cfg.state_file, cursor)
            # E16: memory_pressure — ariel-side emitter (L1 ring growth with hysteresis).
            size = len(mem.l1.get_full()) if mem is not None and hasattr(mem, "l1") and hasattr(mem.l1, "get_full") else 0
            if size > 40 and size - last_pressure >= 10:
                await dispatch("memory_pressure", cfg.layer, cfg.user_id, {"l1_size": size}, mem, graph, rag)
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
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            if poll is not None:
                await poll(cfg.poll_seconds)
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=cfg.poll_seconds)
    finally:
        source.close()
