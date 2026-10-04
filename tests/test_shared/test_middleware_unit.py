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


def test_rate_limit_reason_carries_the_recognizable_prefix():
    """Callers tell "not now" from "no" by this prefix.

    The daemon holds its cursor on a rate-limit block, because dropping the event
    would lose the message permanently. That decision reads the reason string, so
    the prefix is a contract and not a label.
    """
    from shared.middleware import RATE_LIMIT_BLOCK_PREFIX, RateLimitMiddleware

    limiter = RateLimitMiddleware(max_per_minute=1)
    ctx1 = MiddlewareContext(tool_name="new_message", user_id="u")
    asyncio.run(limiter.process(ctx1, _handler))
    ctx2 = MiddlewareContext(tool_name="new_message", user_id="u")
    asyncio.run(limiter.process(ctx2, _handler))

    assert ctx2.blocked is True
    assert ctx2.block_reason.startswith(RATE_LIMIT_BLOCK_PREFIX)


def test_rate_limit_is_configurable_and_can_be_switched_off(monkeypatch):
    """A hard 100/min silently discarded a live replay's work.

    Two escapes: a configured ceiling, and 0-or-less to disable the limiter for
    the deliberate case of replaying history through a loop that is not an
    external client.
    """
    from shared.middleware import RateLimitMiddleware

    # ceiling of 2
    limiter = RateLimitMiddleware(max_per_minute=2)
    for _ in range(2):
        ctx = MiddlewareContext(tool_name="t", user_id="u")
        asyncio.run(limiter.process(ctx, _handler))
        assert ctx.blocked is False
    third = MiddlewareContext(tool_name="t", user_id="u")
    asyncio.run(limiter.process(third, _handler))
    assert third.blocked is True

    # 0 disables it entirely
    off = RateLimitMiddleware(max_per_minute=0)
    for _ in range(250):
        ctx = MiddlewareContext(tool_name="t", user_id="u")
        asyncio.run(off.process(ctx, _handler))
        assert ctx.blocked is False, "0/min must mean unlimited, not 'block everything'"


def test_rate_limit_reads_the_config_and_falls_back_on_a_broken_one(monkeypatch):
    """The ceiling comes from config; a broken config must not fail open.

    The limiter protects the memory store from a runaway caller, so an unreadable
    config falls back to the original 100 rather than removing the protection.
    """
    from shared.middleware import RateLimitMiddleware

    class _Cfg:
        def __init__(self, value):
            self.value = value

        def get(self, section, key, default=None):
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    import config as config_module

    monkeypatch.setattr(config_module, "config", _Cfg(3))
    assert RateLimitMiddleware()._limit() == 3

    monkeypatch.setattr(config_module, "config", _Cfg(RuntimeError("boom")))
    assert RateLimitMiddleware()._limit() == 100
