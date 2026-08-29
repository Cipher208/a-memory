# tests/test_autohooks/test_daemon.py
"""Daemon loop: dispatch payload, cursor persistence, baseline, stop (spec S4)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from autohooks.config import AgentConfig, FieldMap, SourceConfig
from autohooks.daemon import load_cursor, run_daemon, save_cursor
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
