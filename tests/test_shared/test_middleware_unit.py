"""Tests for shared/middleware.py — essential behavior."""

import asyncio

from shared.middleware import (
    AuditMiddleware,
    DedupMiddleware,
    ImportanceGateMiddleware,
    MiddlewareContext,
    MiddlewarePipeline,
)


async def _handler(c):
    return {"ok": True}


def test_gate_blocks_low():
    gate = ImportanceGateMiddleware()
    ctx = MiddlewareContext(args={"value": "hi"}, tool_name="memory_user_remember")
    asyncio.run(gate.process(ctx, _handler))
    assert ctx.blocked is True


def test_gate_allows_high():
    gate = ImportanceGateMiddleware()
    ctx = MiddlewareContext(
        args={
            "value": "This is a critical and important decision about our architecture that affects production systems and requires immediate attention"
        },
        tool_name="memory_user_remember",
    )
    asyncio.run(gate.process(ctx, _handler))
    assert ctx.blocked is False


def test_dedup_catches_duplicates():
    dedup = DedupMiddleware()
    ctx1 = MiddlewareContext(tool_name="t", user_id="u", args={"k": "v"})
    asyncio.run(dedup.process(ctx1, _handler))

    ctx2 = MiddlewareContext(tool_name="t", user_id="u", args={"k": "v"})
    asyncio.run(dedup.process(ctx2, _handler))
    assert ctx2.metadata.get("deduped") is True


def test_pipeline_runs():
    pipe = MiddlewarePipeline()

    class Count:
        name = "count"

        async def process(self, ctx, next_fn):
            ctx.metadata["count"] = True
            return await next_fn(ctx)

    pipe.add(Count())
    ctx = MiddlewareContext()
    result = asyncio.run(pipe.execute(ctx, _handler))
    assert result == {"ok": True}
    assert ctx.metadata.get("count") is True


def test_audit_sets_metadata():
    audit = AuditMiddleware()
    ctx = MiddlewareContext()
    asyncio.run(audit.process(ctx, _handler))
    assert "elapsed" in ctx.metadata
