# tests/test_autohooks/test_daemon.py
"""Daemon loop: dispatch payload, cursor persistence, baseline, stop (spec S4)."""

from __future__ import annotations

import json
import logging
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
    async def _dispatch(event, layer, user_id, payload, mem, graph, rag=None):
        raise ValueError("unknown event: 'bogus'")

    src = _FakeSource(batches=[Batch(messages=[Message(sender="u", text="x", ts=None, source_id=1)], cursor=1)])
    with pytest.raises(ValueError, match="unknown event"):
        await run_daemon(_cfg(tmp_path), src, mem=None, graph=None, rag=None, max_iterations=1, dispatch=_dispatch)


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

    cowagent's `merge_conversations_to_global` folds each secondary agent's
    database into the default one and re-issues message ids on the way in
    (`id -> NULL so the global file re-issues AUTOINCREMENT ids`), and its
    schema rebuild recreates the messages table outright. A saved cursor then
    counts ids that no longer exist; `WHERE id > cursor` matches nothing and the
    daemon looks perfectly healthy while dispatching nothing -- which is exactly
    what one live base did for a week.

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
