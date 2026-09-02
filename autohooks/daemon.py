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
from typing import TYPE_CHECKING, Any

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


def load_cursor(state_file: Path) -> int | None:
    if not state_file.exists():
        return None
    return int(json.loads(state_file.read_text(encoding="utf-8"))["cursor"])


def save_cursor(state_file: Path, cursor: int) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")


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
) -> None:
    """Run the poll loop until SIGTERM/SIGINT (or max_iterations for tests)."""
    from hooks.external import dispatch_event

    dispatch = dispatch or dispatch_event
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
        while not stop.is_set():
            batch = source.fetch_after(cursor, cfg.batch_limit)
            for msg in batch.messages:
                result = await dispatch(
                    "new_message",
                    cfg.layer,
                    cfg.user_id,
                    {"text": msg.text, "sender": msg.sender, "ts": msg.ts, "source_msg_id": msg.source_id},
                    mem,
                    graph,
                    rag,
                )
                logger.debug("dispatched msg %s: %s", msg.source_id, result)
            if batch.messages:
                cursor = batch.cursor
                save_cursor(cfg.state_file, cursor)
            # E16: memory_pressure — ariel-side emitter (L1 ring growth with hysteresis).
            size = len(mem.l1.get_full()) if mem is not None and hasattr(mem, "l1") and hasattr(mem.l1, "get_full") else 0
            if size > 40 and size - last_pressure >= 10:
                await dispatch("memory_pressure", cfg.layer, cfg.user_id, {"l1_size": size}, mem, graph, rag)
                last_pressure = size
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
