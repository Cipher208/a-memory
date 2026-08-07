"""Tests for importance middleware using ImportanceScorer."""

import pytest

from shared.middleware import ImportanceGateMiddleware, MiddlewareContext, MiddlewarePipeline


@pytest.mark.asyncio
async def test_bypass_on_noise():
    mw = ImportanceGateMiddleware(threshold=0.3)
    ctx = MiddlewareContext(tool_name="memory_user_remember", args={"text": "ok"})
    captured = {}

    async def handler(c):
        captured["called"] = True
        return "ok"

    await MiddlewarePipeline().add(mw).execute(ctx, handler)
    assert not captured.get("called")
    assert ctx.blocked
    assert "below_importance_threshold" in ctx.block_reason


@pytest.mark.asyncio
async def test_passes_technical_content():
    mw = ImportanceGateMiddleware(threshold=0.3)
    ctx = MiddlewareContext(
        tool_name="memory_user_remember",
        args={"text": "Redis cluster JWT auth critical production incident"},
    )
    captured = {}

    async def handler(c):
        captured["called"] = True
        captured["importance"] = c.args.get("importance")
        return "ok"

    await MiddlewarePipeline().add(mw).execute(ctx, handler)
    assert captured.get("called")
    assert captured.get("importance") > 0.4


@pytest.mark.asyncio
async def test_passes_instruction_kind():
    mw = ImportanceGateMiddleware(threshold=0.3)
    ctx = MiddlewareContext(
        tool_name="memory_user_remember",
        args={"text": "запомни", "memory_kind": "instruction"},
    )
    mw.memory_kind_hint = "instruction"
    captured = {}

    async def handler(c):
        captured["called"] = True

    await MiddlewarePipeline().add(mw).execute(ctx, handler)
    assert captured.get("called")


@pytest.mark.asyncio
async def test_signal_breakdown_in_metadata():
    mw = ImportanceGateMiddleware(threshold=0.0)
    ctx = MiddlewareContext(
        tool_name="memory_user_remember",
        args={"text": "redis cluster crash on jwt endpoint"},
    )

    async def handler(c):
        return "ok"

    await MiddlewarePipeline().add(mw).execute(ctx, handler)
    assert "importance_signals" in ctx.metadata
    sig = ctx.metadata["importance_signals"]
    assert sig.tech_keyword > 0
