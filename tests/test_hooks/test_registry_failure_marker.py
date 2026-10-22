"""The hook registry must say whether a failed handler is worth retrying.

`hooks/registry.py` catches every handler exception and reports it as a
result, so the caller never sees an exception. That is what made the
daemon's promise of at-least-once delivery a lie: the daemon read the
result "one handler failed" as "the message was handled" and advanced the
cursor past a message that was never written. The source is a chat log that
cannot be re-read from the middle, so that loss was permanent.

The marker is explicit and machine-readable: every failed entry carries
`transient`, True for a failure that clears by itself (a busy database) and
False for a handler bug. The daemon reads the marker and holds the cursor
only for the transient case, so a poison message cannot stall the whole
ingestion.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from hooks.registry import HookHandler, HookRegistry


def _registry_with(handler: Any) -> HookRegistry:
    registry = HookRegistry()
    registry.register(
        HookHandler(
            func=handler,
            name="new_message",
            layer="user",
            is_async=True,
            takes_mem=False,
            takes_graph=False,
        )
    )
    return registry


async def test_a_busy_database_is_marked_transient() -> None:
    """A sqlite error is transient: the next tick may deliver the message."""

    async def _boom(ctx: dict[str, Any]) -> dict[str, Any]:
        raise sqlite3.OperationalError("database is locked")

    result = await _registry_with(_boom).fire("new_message", "user", {"text": "x"})

    assert result["results"], result
    entry = result["results"][0]
    assert entry.get("transient") is True, f"a busy database must be marked transient: {entry}"
    assert entry.get("error") == "database is locked", entry


async def test_a_handler_bug_is_marked_permanent() -> None:
    """Anything that is not a sqlite error is a bug in the handler.

    It must be distinguishable from a transient failure, because retrying it
    forever would stall the ingestion on one poison message.
    """

    async def _boom(ctx: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("a bug in the handler")

    result = await _registry_with(_boom).fire("new_message", "user", {"text": "x"})

    assert result["results"], result
    entry = result["results"][0]
    assert entry.get("transient") is False, f"a handler bug must not be marked transient: {entry}"
    assert entry.get("error") == "a bug in the handler", entry


async def test_a_successful_handler_carries_no_error_entry() -> None:
    """The marker belongs to failures only; a delivered message has no error."""

    async def _ok(ctx: dict[str, Any]) -> dict[str, Any]:
        return {"saved": True}

    result = await _registry_with(_ok).fire("new_message", "user", {"text": "x"})

    assert result["results"] == [{"saved": True}], result


def test_the_fire_docstring_documents_the_transient_marker() -> None:
    """The contract is written down where the results are produced.

    A marker nobody documents is a marker the next reader has to guess at.
    """
    doc = HookRegistry.fire.__doc__ or ""
    assert "transient" in doc, "the `fire` docstring must document the transient marker"
    assert "sqlite3.Error" in doc, "the `fire` docstring must say which failures are transient"
