# tests/test_autohooks/test_daemon.py
"""Daemon loop: dispatch payload, cursor persistence, baseline, stop (spec S4)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from autohooks.config import AgentConfig, FieldMap, SourceConfig
from autohooks.daemon import load_cursor, run_daemon, save_cursor, source_went_backwards
from autohooks.source import Batch, Message, SqliteSource

if TYPE_CHECKING:
    from pathlib import Path


class _FakeSource(SqliteSource):
    """Bypasses SQL; replays scripted batches. Overrides the whole surface."""

    def __init__(self, batches: list[Batch], max_id: int = 0) -> None:
        self._batches = batches
        self._max = max_id

    def close(self) -> None:
        pass

    def max_id(self) -> int:  # type: ignore[override]
        return self._max

    def fetch_after(self, cursor: int, limit: int) -> Batch:  # type: ignore[override]
        return self._batches.pop(0) if self._batches else Batch(messages=[], cursor=cursor)


def _cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        data_dir=tmp_path / "data",
        user_id="u1",
        layer="user",
        source=SourceConfig(
            driver="sqlite",
            path=tmp_path / "conv.db",
            table="messages",
            cursor_column="id",
            order_by="id",
            role=FieldMap(column="role"),
            text=FieldMap(column="content"),
            ts=None,
            filter=None,
        ),
        poll_seconds=0.01,
        state_file=tmp_path / "cursor.json",
    )


async def test_first_run_baseline_no_replay(tmp_path: Path) -> None:
    dispatched: list[dict[str, Any]] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        dispatched.append(payload)
        return {"results": [], "handler_count": 0}

    src = _FakeSource(batches=[], max_id=42)
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)
    assert dispatched == []
    assert load_cursor(tmp_path / "cursor.json") == 42


async def test_daemon_sweeps_dangling_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon loop runs the central liveness sweep (stale-WAL-snapshot guard,
    2026-09-16): first iteration prunes via the graph's connection manager."""
    calls: list[Any] = []

    async def fake_prune(cm: Any) -> int:
        calls.append(cm)
        return 7

    monkeypatch.setattr("lifecycle.graph_sanitation.prune_dangling_edges", fake_prune)

    class _Graph:
        _cm = "stub-cm"

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"results": [], "handler_count": 0}

    src = _FakeSource(batches=[], max_id=1)
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=_Graph(), rag=None, max_iterations=1, dispatch=_dispatch)
    assert calls == ["stub-cm"]


async def test_daemon_logs_connection_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The daemon loop reads connection health — the wiring, not just the function.

    Written after M4 taught the lesson on the server side: a test that calls the
    helper directly stays green when the call is deleted from the loop, which is how
    a diagnostic ends up written and forgotten. This drives `run_daemon` itself.
    """
    import logging

    calls: list[int] = []

    async def fake_health() -> dict[str, Any]:
        calls.append(1)
        return {"connections": 1, "dbs": ["memory.db"], "leaked_workers": 0, "open_db_fds": 41, "unmanaged_fds": 40}

    monkeypatch.setattr("shared.connection.connection_health", fake_health)

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"results": [], "handler_count": 0}

    src = _FakeSource(batches=[], max_id=1)
    with caplog.at_level(logging.INFO):
        await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert calls, "the daemon loop never read connection health — nothing will be logged"
    line = next((r.getMessage() for r in caplog.records if "connection health" in r.getMessage()), None)
    assert line is not None, [r.getMessage() for r in caplog.records]
    # The two counts must both be in the line: they disagreeing is the finding.
    assert "open_db_fds=41" in line and "unmanaged_fds=40" in line, line


async def test_health_failure_does_not_stop_the_poll_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken diagnostic must not take the daemon down with it."""

    async def exploding_health() -> dict[str, Any]:
        raise RuntimeError("health blew up")

    monkeypatch.setattr("shared.connection.connection_health", exploding_health)

    disposed: list[int] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        disposed.append(1)
        return {"results": [], "handler_count": 0}

    src = _FakeSource(
        batches=[
            Batch(
                messages=[Message(sender="user", text="решение по architecture", ts=1.5, source_id=7)],
                cursor=7,
            )
        ]
    )
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert disposed, "the loop stopped early because the health reading raised"


async def test_dispatch_payload_and_cursor_advance(tmp_path: Path) -> None:
    dispatched: list[dict[str, Any]] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        dispatched.append({"event": event, "layer": layer, "user_id": user_id, **payload})
        return {"results": [{"saved": True}], "handler_count": 1}

    src = _FakeSource(batches=[Batch(messages=[Message(sender="user", text="важное решение по архитектуре", ts=1.5, source_id=7)], cursor=7)])
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)
    assert len(dispatched) == 1
    assert dispatched[0]["event"] == "new_message"
    assert dispatched[0]["user_id"] == "u1"
    assert dispatched[0]["text"] == "важное решение по архитектуре"
    assert dispatched[0]["sender"] == "user"
    assert dispatched[0]["source_msg_id"] == 7
    assert load_cursor(tmp_path / "cursor.json") == 7


async def test_cursor_saved_once_per_batch_not_per_message(tmp_path: Path) -> None:
    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"results": [], "handler_count": 0}

    msgs = [Message(sender="user", text=f"m{i}", ts=None, source_id=i) for i in range(1, 4)]
    src = _FakeSource(batches=[Batch(messages=msgs, cursor=3)])
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)
    assert json.loads((tmp_path / "cursor.json").read_text(encoding="utf-8")) == {"cursor": 3}


async def test_unknown_event_valueerror_propagates(tmp_path: Path) -> None:
    """An unknown event type is a programming error, and it must surface.

    The daemon contains DATABASE failures, not programming errors: swallowing a
    `ValueError` raised for an unknown event type would turn a typo in the
    dispatch table into a silent retry loop that never makes progress and never
    says why. Only `sqlite3.Error` is contained.
    """

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        raise ValueError("unknown event: 'bogus'")

    src = _FakeSource(batches=[Batch(messages=[Message(sender="u", text="x", ts=None, source_id=1)], cursor=1)])
    with pytest.raises(ValueError, match="unknown event"):
        await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)


async def test_a_locked_database_does_not_kill_the_daemon(tmp_path: Path, caplog) -> None:
    """`database is locked` must not take the daemon process down.

    The daemon had no `except` at all around `dispatch(...)`: a single busy
    write killed the process, systemd restarted it ten seconds later, and the
    journal filled with the same failure. The message is retried on the next
    tick instead, and the cursor does not move past it -- the at-least-once
    promise this module documents.
    """
    attempts: list[int] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        attempts.append(payload["source_msg_id"])
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"results": [], "handler_count": 0}

    msgs = [Message(sender="user", text="a message worth keeping", ts=None, source_id=1)]
    src = _CursorSource(msgs)
    save_cursor(tmp_path / "cursor.json", 0)

    with caplog.at_level(logging.WARNING, logger="autohooks.daemon"):
        await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=2, dispatch=_dispatch)

    # The failed message was attempted again on the next tick and then written.
    assert attempts == [1, 1], "the failed message must be retried, not dropped"
    assert load_cursor(tmp_path / "cursor.json") == 1, "the cursor must advance only once the message went through"

    warnings = [r.getMessage() for r in caplog.records if "database is locked" in r.getMessage()]
    assert warnings, "the failure must be reported"
    line = warnings[0]
    assert "source_msg_id=1" in line, line
    assert "OperationalError" in line, line


async def test_a_failed_message_does_not_advance_the_cursor(tmp_path: Path, caplog) -> None:
    """A message that failed is not handled: the cursor stays behind it."""
    attempts: list[int] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        attempts.append(payload["source_msg_id"])
        raise sqlite3.OperationalError("database is locked")

    msgs = [Message(sender="user", text="first", ts=None, source_id=1), Message(sender="user", text="second", ts=None, source_id=2)]
    src = _CursorSource(msgs)
    save_cursor(tmp_path / "cursor.json", 0)

    with caplog.at_level(logging.WARNING, logger="autohooks.daemon"):
        await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert attempts == [1], "the batch must stop at the failed message"
    assert load_cursor(tmp_path / "cursor.json") == 0, "the cursor must not move past a failed message"


async def test_a_non_database_error_propagates(tmp_path: Path) -> None:
    """Only `sqlite3.Error` is contained; every other error surfaces.

    The containment exists for one failure mode -- a busy database -- and it
    must not become a blanket shield. A `TypeError` from `dispatch(...)` is a
    programming error: swallowing it would leave the daemon polling forever
    over a message it can never handle, with the reason buried in a warning.
    """

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        raise TypeError("dispatch called with the wrong shape")

    src = _FakeSource(batches=[Batch(messages=[Message(sender="u", text="x", ts=None, source_id=1)], cursor=1)])
    with pytest.raises(TypeError, match="wrong shape"):
        await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)


async def test_a_resolve_failure_propagates(tmp_path: Path) -> None:
    """A failure to resolve the target layer is a configuration error.

    `resolve(d_layer)` sits OUTSIDE the containment, on purpose: it reads
    configuration and opens the per-layer clients, so a failure there is not a
    busy database but a broken setup. It must surface instead of being retried
    forever with the cursor held behind an undeliverable message.
    """
    from dataclasses import replace

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"results": [], "handler_count": 0}

    def _resolve(layer: str) -> tuple[Any, Any, Any]:
        raise RuntimeError("no client for layer 'agent'")

    cfg = replace(_cfg(tmp_path), persona_owner=True)
    src = _CursorSource([Message(sender="assistant", text="a declaration", ts=None, source_id=1)])
    save_cursor(tmp_path / "cursor.json", 0)

    with pytest.raises(RuntimeError, match="no client for layer"):
        await run_daemon(cfg, src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch, resolve=_resolve)


async def test_poll_sleeps_between_iterations(tmp_path: Path) -> None:
    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"results": [], "handler_count": 0}

    slept: list[float] = []

    async def _poll(seconds: float) -> None:
        slept.append(seconds)

    src = _FakeSource(batches=[])
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=3, poll=_poll, dispatch=_dispatch)
    # N iterations → N-1 inter-iteration sleeps (no sleep before exiting).
    assert slept == [0.01, 0.01]


def test_cursor_roundtrip(tmp_path: Path) -> None:
    assert load_cursor(tmp_path / "absent.json") is None
    save_cursor(tmp_path / "state" / "cursor.json", 15)
    assert load_cursor(tmp_path / "state" / "cursor.json") == 15


async def test_persona_owner_assistant_routes_agent_layer(tmp_path: Path) -> None:
    """S10 speaker axis: persona_owner client + assistant sender → agent layer,
    payload carries role + persona_owner for the declared-canon channel."""
    from dataclasses import replace

    dispatched: list[dict[str, Any]] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        dispatched.append({"event": event, "layer": layer, **payload})
        return {"results": [], "handler_count": 0}

    cfg = replace(_cfg(tmp_path), persona_owner=True)
    src = _FakeSource(
        batches=[
            Batch(
                messages=[
                    Message(sender="assistant", text="Госпожа закрепила канон обращения к хозяйке", ts=2.5, source_id=9),
                    Message(sender="user", text="да, госпожа", ts=2.6, source_id=10),
                ],
                cursor=10,
            )
        ]
    )
    await run_daemon(cfg, src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch, resolve=lambda layer: (None, None, None))
    assert len(dispatched) == 2
    assert dispatched[0]["layer"] == "agent"
    assert dispatched[0]["role"] == "assistant" and dispatched[0]["persona_owner"] is True
    assert dispatched[1]["layer"] == "user"
    assert dispatched[1]["role"] == "user"


async def test_no_persona_owner_keeps_single_layer(tmp_path: Path) -> None:
    dispatched: list[dict[str, Any]] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        dispatched.append({"event": event, "layer": layer, **payload})
        return {"results": [], "handler_count": 0}

    src = _FakeSource(batches=[Batch(messages=[Message(sender="assistant", text="reply body here for harvest", ts=2.5, source_id=9)], cursor=9)])
    await run_daemon(
        _cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch, resolve=lambda layer: (None, None, None)
    )
    assert dispatched[0]["layer"] == "user"
    assert dispatched[0]["persona_owner"] is False


class _CursorSource(SqliteSource):
    """Serves messages with source_id > cursor, like the real SQL does.

    _FakeSource pops one scripted batch per call, which cannot express a retry:
    a retry has to fetch the SAME messages again once the cursor did not move.

    ``max_id`` defaults to the largest id actually on offer, because a real
    source cannot answer lower -- SqliteSource computes it as
    ``SELECT max(cursor_column) FROM table``. It used to default to 0 while
    serving ids 1..3, i.e. a source claiming its newest row was older than rows
    it then handed out; the daemon's "source went backwards" check correctly
    calls that impossible. Tests here that want to read from the very beginning
    now start from a saved cursor of 0, which is what a daemon that has consumed
    nothing actually looks like, instead of relying on a contradictory max_id.
    """

    def __init__(self, messages: list[Message], max_id: int | None = None) -> None:
        self._msgs = list(messages)
        self._max = max((m.source_id for m in self._msgs), default=0) if max_id is None else max_id

    def close(self) -> None:
        pass

    def max_id(self) -> int:  # type: ignore[override]
        return self._max

    def fetch_after(self, cursor: int, limit: int) -> Batch:  # type: ignore[override]
        rows = [m for m in self._msgs if m.source_id > cursor][:limit]
        return Batch(messages=rows, cursor=rows[-1].source_id if rows else cursor)


async def test_rate_limited_message_holds_the_cursor_and_is_retried(tmp_path: Path) -> None:
    """A rate-limit block means "not now", so the cursor must not move past it.

    Measured on a live replay: 2590 rate-limit blocks lost 63 of 142 substantive
    messages, each leaving no journal row, no dispatch-log row and no warning.
    The old code advanced `cursor = batch.cursor` unconditionally, which is what
    made the loss permanent -- the source is a chat log that cannot be re-read
    from the middle.
    """
    seen: list[Any] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        seen.append(payload["source_msg_id"])
        if payload["source_msg_id"] == 2 and seen.count(2) == 1:
            return {"skipped": True, "reason": "Rate limit exceeded (100/min)"}
        return {"results": [], "handler_count": 0}

    msgs = [Message(sender="assistant", text=f"m{i}", ts=None, source_id=i) for i in (1, 2, 3)]
    src = _CursorSource(msgs)
    save_cursor(tmp_path / "cursor.json", 0)  # a daemon that has consumed nothing yet
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=2, dispatch=_dispatch)

    # Message 2 appears twice: the second attempt is the retry the held cursor
    # made possible. Message 3 is only reached after 2 actually went through.
    assert seen == [1, 2, 2, 3]
    assert load_cursor(tmp_path / "cursor.json") == 3


async def test_non_rate_limit_skips_still_advance_the_cursor(tmp_path: Path) -> None:
    """Every other decline is a verdict on the event, so the cursor moves on.

    Holding the cursor for an unimportant or duplicate event would stall the
    daemon forever on a message that can never be accepted.
    """

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"skipped": True, "reason": "below_importance_threshold(0.10, threshold=0.50)"}

    msgs = [Message(sender="user", text=f"m{i}", ts=None, source_id=i) for i in (1, 2, 3)]
    src = _CursorSource(msgs)
    save_cursor(tmp_path / "cursor.json", 0)
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)
    assert load_cursor(tmp_path / "cursor.json") == 3


async def test_empty_text_is_skipped_before_it_costs_anything(tmp_path: Path) -> None:
    """Empty rows are never dispatched, and the cursor still clears them.

    A chat database storing one row per streamed chunk produces thousands of
    empty assistant rows (2034 in one live log). They can never become a memory
    -- `_new_message` answers `no_text_or_mem` -- but they consumed a rate-limit
    slot first, because the pipeline runs ahead of the handler, and that is what
    crowded the real messages out of the budget.
    """
    seen: list[Any] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        seen.append(payload["source_msg_id"])
        return {"results": [], "handler_count": 0}

    msgs = [
        Message(sender="assistant", text="", ts=None, source_id=1),
        Message(sender="assistant", text="   \n  ", ts=None, source_id=2),
        Message(sender="assistant", text="настоящее сообщение", ts=None, source_id=3),
    ]
    src = _CursorSource(msgs)
    save_cursor(tmp_path / "cursor.json", 0)
    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert seen == [3], "empty rows must not reach the dispatcher at all"
    assert load_cursor(tmp_path / "cursor.json") == 3, "but the cursor must still move past them"


async def test_a_source_that_went_backwards_rewinds_the_cursor_and_says_so(tmp_path: Path) -> None:
    """A renumbered source must not leave the daemon polling into a void.

    cowagent's `migrate_conversations_to_global` (via `_merge_secondary_agents` ->
    `_merge_one_agent`) folds each secondary agent's database into the default one
    and re-issues message ids on the way in (`id -> NULL so the global file
    re-issues AUTOINCREMENT ids`). A saved cursor then counts ids that no longer
    exist; `WHERE id > cursor` matches nothing and the daemon looks perfectly
    healthy while dispatching nothing.

    This is the PREVENTIVE case, not a reported one: no live occurrence has been
    observed. The base that first drew attention here was stalled for another
    reason -- its platform stopped writing rows -- and its cursor equalled its max
    id, so this guard would not have fired on it.

    Note also that only the MERGE re-issues ids. The multi-agent schema rebuild
    recreates `sessions`/`messages` under composite keys but copies the id
    explicitly, so it preserves them; an earlier version of this docstring
    claimed otherwise.

    Rewinding is the safe response: L0 dedups by content hash, so re-reading is
    idempotent, whereas never reading again is permanent.
    """
    seen: list[Any] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        seen.append(payload["source_msg_id"])
        return {"results": [], "handler_count": 0}

    # The source now only holds ids 1..3, but the saved cursor is at 900 -- what a
    # pre-merge cursor looks like against a renumbered file.
    src = _CursorSource([Message(sender="user", text=f"m{i}", ts=None, source_id=i) for i in (1, 2, 3)])
    save_cursor(tmp_path / "cursor.json", 900)

    await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)

    assert seen == [1, 2, 3], "the rewound daemon must re-read the source instead of sitting idle"
    assert load_cursor(tmp_path / "cursor.json") == 3


async def test_a_source_that_went_backwards_warns_once_and_does_not_loop(tmp_path: Path, caplog) -> None:
    """The rewind is saved, so it is reported once rather than every poll.

    A warning that repeats on every iteration of a 15-second loop is noise an
    operator learns to ignore, which is how the original silence went unnoticed.
    """
    src = _CursorSource([Message(sender="user", text="x", ts=None, source_id=2)])
    save_cursor(tmp_path / "cursor.json", 500)

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        return {"results": [], "handler_count": 0}

    with caplog.at_level(logging.WARNING, logger="autohooks.daemon"):
        await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=3, dispatch=_dispatch)

    warnings = [r for r in caplog.records if "went backwards" in r.getMessage()]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
    assert load_cursor(tmp_path / "cursor.json") == 2


def test_a_non_integer_cursor_column_is_left_alone() -> None:
    """A TEXT cursor key must not make the guard raise inside the poll loop.

    `cursor_column` is whatever the config names, and SqliteSource orders it as
    stored, so a TEXT key is a legitimate configuration even though every source
    in the house uses an integer one. Comparing it to an integer raises TypeError,
    which would kill the daemon on its first poll -- a worse outcome than the
    missed reset the guard exists to prevent.
    """
    assert source_went_backwards("msg_abc", 64763) is False
    # An integer behind an integer is still the real case it must catch.
    assert source_went_backwards(3, 900) is True
    assert source_went_backwards(900, 3) is False
    assert source_went_backwards(5, 5) is False


# --------------------------------------------------------------------------
# The delivery contract, driven through the REAL dispatch path.
#
# The bug these pin lived between two components: `hooks/registry.py` catches
# a handler's exception and reports it as a result, and the daemon read that
# result as a delivery. An injected `dispatch=` cannot reproduce it -- the
# injected callable never sees the swallowed exception -- so the daemon's own
# `dispatch_event` is used here.
# --------------------------------------------------------------------------


def _real_dispatch_with(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Register a handler for `new_message` and let the daemon dispatch for real.

    The dedup middleware keys on the payload and would swallow the RETRY
    inside its five-second window. That is a different defect -- a declined
    event, not a failed delivery -- and it would hide what these tests are
    about, so the window is switched off for the duration of the test.
    """
    from hooks.registry import HookHandler, hook_registry
    from shared.middleware import DedupMiddleware, default_pipeline

    hook_registry.register(
        HookHandler(
            func=handler,
            name="new_message",
            layer="user",
            is_async=True,
            takes_mem=False,
            takes_graph=False,
        )
    )
    for mw in default_pipeline._middlewares:
        if isinstance(mw, DedupMiddleware):
            monkeypatch.setattr(mw, "_window", 0)


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


async def test_a_transient_hook_failure_holds_the_cursor(tmp_path: Path, caplog, monkeypatch) -> None:
    """A failed delivery must leave the cursor BEHIND the message.

    The registry catches the handler's `database is locked` and reports it as
    a result, so the daemon never saw an exception: it read the result as a
    delivery, advanced the cursor past the message, and the message was lost
    for good -- the source is a chat log that cannot be re-read from the
    middle. The cursor must stay where it was, and the failure must be
    reported with the source id and the error.
    """
    attempts: list[int] = []

    async def _handler(ctx: dict[str, Any]) -> dict[str, Any]:
        attempts.append(int(ctx.get("source_msg_id", -1)))
        raise sqlite3.OperationalError("database is locked")

    _real_dispatch_with(monkeypatch, _handler)

    msgs = [Message(sender="user", text="a message worth keeping", ts=None, source_id=1)]
    src = _CursorSource(msgs)
    save_cursor(tmp_path / "cursor.json", 0)

    cfg = replace(_cfg(tmp_path), user_id="u-transient-hold")
    with caplog.at_level(logging.WARNING, logger="autohooks.daemon"):
        await run_daemon(cfg, src, mem=None, graph=None, rag=None, max_iterations=1, poll=_no_wait)

    assert attempts == [1], "the message must reach the handler"
    assert load_cursor(tmp_path / "cursor.json") == 0, "the cursor must stay behind a message that was not delivered"

    warnings = [r.getMessage() for r in caplog.records if "source_msg_id=1" in r.getMessage()]
    assert warnings, "the failed delivery must be reported"
    assert "database is locked" in warnings[0], warnings[0]


async def test_a_transient_hook_failure_is_retried_on_the_next_tick(tmp_path: Path, monkeypatch) -> None:
    """The held cursor must make the next tick pick the same message up again.

    Holding the cursor is only useful if something reads it again: the
    message is offered once more, and this time the handler works, so the
    cursor finally moves.
    """
    attempts: list[int] = []

    async def _handler(ctx: dict[str, Any]) -> dict[str, Any]:
        attempts.append(int(ctx.get("source_msg_id", -1)))
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"saved": True}

    _real_dispatch_with(monkeypatch, _handler)

    msgs = [Message(sender="user", text="a message worth keeping", ts=None, source_id=1)]
    src = _CursorSource(msgs)
    save_cursor(tmp_path / "cursor.json", 0)

    cfg = replace(_cfg(tmp_path), user_id="u-transient-retry")
    await run_daemon(cfg, src, mem=None, graph=None, rag=None, max_iterations=2, poll=_no_wait)

    assert attempts == [1, 1], "the failed message must be attempted again, not dropped"
    assert load_cursor(tmp_path / "cursor.json") == 1, "the cursor must advance once the message went through"


async def test_a_permanent_hook_failure_still_advances_the_cursor(tmp_path: Path, monkeypatch) -> None:
    """A handler bug is permanent: the message is handled and the cursor moves on.

    Retrying a bug forever would stall the whole ingestion on one poison
    message -- the daemon would poll over it silently, forever. The
    classification the daemon acts on is checked here through the REAL
    dispatch path, because that is the only place the marker exists: the
    registry writes it, the daemon reads it.
    """
    attempts: list[int] = []

    async def _handler(ctx: dict[str, Any]) -> dict[str, Any]:
        attempts.append(int(ctx.get("source_msg_id", -1)))
        raise ValueError("a bug in the handler")

    _real_dispatch_with(monkeypatch, _handler)

    cfg = replace(_cfg(tmp_path), user_id="u-permanent")

    from hooks.external import dispatch_event

    probe = await dispatch_event(
        "new_message",
        "user",
        cfg.user_id,
        {"text": "a different message", "source_msg_id": 99},
        None,
        None,
    )
    assert probe["results"][0].get("transient") is False, f"a handler bug must not be transient: {probe}"

    msgs = [Message(sender="user", text="a message worth keeping", ts=None, source_id=1)]
    src = _CursorSource(msgs)
    save_cursor(tmp_path / "cursor.json", 0)

    await run_daemon(cfg, src, mem=None, graph=None, rag=None, max_iterations=2, poll=_no_wait)

    assert attempts == [99, 1], "a permanent failure must be attempted once, not retried forever"
    assert load_cursor(tmp_path / "cursor.json") == 1, "the cursor must move past a message that can never be delivered"


async def test_a_locked_source_read_does_not_kill_the_daemon(tmp_path: Path, caplog) -> None:
    """A busy SOURCE must not take the daemon process down.

    The source is a chat log written by ANOTHER process, so `database is
    locked` there is the one place the daemon can plausibly die of exactly
    this error. It is contained, reported, and retried on the next tick.
    """
    calls = {"max_id": 0, "fetch": 0}

    class _LockedSource(SqliteSource):
        def __init__(self) -> None:
            self._max = 0

        def max_id(self) -> int:  # type: ignore[override]
            calls["max_id"] += 1
            raise sqlite3.OperationalError("database is locked")

        def fetch_after(self, cursor: int, limit: int) -> Batch:  # type: ignore[override]
            calls["fetch"] += 1
            raise sqlite3.OperationalError("database is locked")

        def close(self) -> None:
            pass

    with caplog.at_level(logging.WARNING, logger="autohooks.daemon"):
        await run_daemon(_cfg(tmp_path), _LockedSource(), mem=None, graph=None, rag=None, max_iterations=2, poll=_no_wait)

    assert calls["max_id"] == 2, "the daemon must keep polling after a busy source"
    warnings = [r.getMessage() for r in caplog.records if "source read failed" in r.getMessage()]
    assert warnings, "the busy source must be reported"
    assert "database is locked" in warnings[0], warnings[0]


async def test_a_locked_source_fetch_does_not_kill_the_daemon(tmp_path: Path, caplog) -> None:
    """The same containment for the READ of the batch itself.

    The baseline read (`max_id`) can succeed while the batch read loses the
    race: the two are separate statements, and the writer that holds the
    lock only has to arrive between them.
    """
    calls = {"fetch": 0}

    class _LockedFetchSource(_CursorSource):
        def fetch_after(self, cursor: int, limit: int) -> Batch:  # type: ignore[override]
            calls["fetch"] += 1
            raise sqlite3.OperationalError("database is locked")

    src = _LockedFetchSource([Message(sender="user", text="x", ts=None, source_id=1)])
    save_cursor(tmp_path / "cursor.json", 0)

    with caplog.at_level(logging.WARNING, logger="autohooks.daemon"):
        await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=2, poll=_no_wait)

    assert calls["fetch"] == 2, "the daemon must keep polling after a busy source"
    assert load_cursor(tmp_path / "cursor.json") == 0, "a batch that was never read must not move the cursor"
    warnings = [r.getMessage() for r in caplog.records if "source read failed" in r.getMessage()]
    assert warnings, "the busy source must be reported"


async def test_a_locked_memory_pressure_dispatch_does_not_kill_the_daemon(tmp_path: Path, caplog) -> None:
    """The memory-pressure dispatch is a DB write too, and it is contained.

    `memory_pressure` asks the store to shed memory; it runs on the same
    base as everything else, so a busy database there is the same failure
    as anywhere else. It must be reported and skipped, not fatal.
    """
    events: list[str] = []

    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        events.append(event)
        if event == "memory_pressure":
            raise sqlite3.OperationalError("database is locked")
        return {"results": [], "handler_count": 0}

    mem = SimpleNamespace(l1=SimpleNamespace(get_full=lambda: [object()] * 45))
    src = _CursorSource([Message(sender="user", text="x", ts=None, source_id=1)])

    with caplog.at_level(logging.WARNING, logger="autohooks.daemon"):
        await run_daemon(_cfg(tmp_path), src, mem=mem, graph=None, rag=None, max_iterations=2, dispatch=_dispatch, poll=_no_wait)

    assert events.count("memory_pressure") == 2, "the pressure event must be retried on the next tick"
    warnings = [r.getMessage() for r in caplog.records if "memory_pressure" in r.getMessage()]
    assert warnings, "the failed pressure dispatch must be reported"
    assert "database is locked" in warnings[0], warnings[0]


def test_the_daemon_does_not_claim_the_base_dies_of_a_locked_database() -> None:
    """The narrative must match the live evidence.

    The victim of `database is locked` on the house base is the MCP SERVER's
    importance sweep; both autohooks units report `NRestarts=0`. The
    daemon's real failure was not dying -- it was LOSING the message.
    """
    import autohooks.daemon as daemon_module
    from pathlib import Path as _Path

    src = _Path(daemon_module.__file__).read_text(encoding="utf-8")
    assert "hermes base dies" not in src, "the daemon must not claim the base dies of a locked database"
    assert "NRestarts=0" in src, "the daemon docstring must state what the live evidence says"
