"""memory_hook MCP tool — thin dispatcher wrapper (spec S6)."""

from __future__ import annotations

import inspect

import pytest

from mcp_server.tools.hooks import memory_hook


@pytest.mark.asyncio
async def test_unknown_event_raises_value_error() -> None:
    from types import SimpleNamespace

    # shell ctx: dispatch_event validates the event before touching app
    fake_ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=None))
    with pytest.raises(ValueError, match="unknown event"):
        await memory_hook(event="nope", payload={}, ctx=fake_ctx)  # type: ignore[arg-type]


def test_tool_has_expected_signature() -> None:
    sig = inspect.signature(memory_hook)
    assert list(sig.parameters) == ["event", "payload", "layer", "user_id", "ctx"]
