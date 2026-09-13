"""P (spec S4): kind-declared think writes L4 regardless of length."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.memory_types import L4_DECLARABLE_KINDS


def _make_ctx():
    """Same mock-app pattern as tests/test_primitives.py."""
    ctx = MagicMock()
    app = MagicMock()
    app.mm = MagicMock()
    app.rate_limiter = MagicMock()
    app.rate_limiter.check = AsyncMock(return_value={"allowed": True})
    app.importance = MagicMock()
    app.importance.score.return_value.score = 0.4
    app.importance.score.return_value.signals.emotional = 0.1
    app.user_graph = MagicMock()
    app.agent_graph = MagicMock()
    app.hook_registry = MagicMock()
    app.hook_registry.fire = AsyncMock(return_value={})
    ctx.request_context.lifespan_context = app
    return ctx, app


@pytest.fixture(autouse=True)
def _reset_canon_rate():
    from shared import canon_rate as _cr

    _cr._canon_ts.clear()
    yield
    _cr._canon_ts.clear()


@pytest.mark.asyncio
async def test_declared_kind_lands_l4_despite_length():
    from mcp_server.tools.primitives.think import think

    ctx, app = _make_ctx()
    mem = app.mm.user_memory.return_value
    mem.remember = AsyncMock(return_value=42)
    mem.l3.save = AsyncMock()

    long_text = ("Протокол обращения закреплён как канон личности: " * 10)[:800]  # far beyond 60 chars
    assert "preference" in L4_DECLARABLE_KINDS
    res = await think(text=long_text, user_id="decl", ctx=ctx, kind="preference", layer="user")

    assert res["status"] == "ok"
    types = [a["type"] for a in res["actions"]]
    assert "L4_declared_canon" in types
    key = mem.remember.call_args[0][0]
    assert key.startswith("canon:preference:")
    assert mem.remember.call_args[0][2] == pytest.approx(0.8)  # floor applied over 0.4


@pytest.mark.asyncio
async def test_undeclared_behavior_unchanged():
    from mcp_server.tools.primitives.think import think

    ctx, app = _make_ctx()
    mem = app.mm.user_memory.return_value
    mem.remember = AsyncMock(return_value=1)
    mem.l3.save = AsyncMock()

    res = await think(text="short note without kind hint", user_id="decl", ctx=ctx, layer="user")
    types = [a["type"] for a in res["actions"]]
    assert "L4_declared_canon" not in types


@pytest.mark.asyncio
async def test_invalid_kind_rejected():
    from mcp_server.tools.primitives.think import think

    ctx, _ = _make_ctx()
    res = await think(text="попытка", user_id="decl", ctx=ctx, kind="banana", layer="user")
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_non_declarable_valid_kind_rejected():
    from mcp_server.tools.primitives.think import think

    ctx, _ = _make_ctx()
    res = await think(text="вопрос", user_id="decl", ctx=ctx, kind="question", layer="user")
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_rate_limit_10_per_hour():
    from mcp_server.tools.primitives.think import think

    for i in range(10):
        ctx, app = _make_ctx()
        mem = app.mm.user_memory.return_value
        mem.remember = AsyncMock(return_value=i + 1)
        mem.l3.save = AsyncMock()
        res = await think(text=f"каноническое правило номер {i} для лимита", user_id="rl", ctx=ctx, kind="rule", layer="user")
        assert "L4_declared_canon" in [a["type"] for a in res["actions"]]

    ctx, app = _make_ctx()
    mem = app.mm.user_memory.return_value
    mem.remember = AsyncMock(return_value=99)
    mem.l3.save = AsyncMock()
    res = await think(text="одиннадцатый декларативный канон за этот час", user_id="rl", ctx=ctx, kind="rule", layer="user")
    types = [a["type"] for a in res["actions"]]
    assert "L4_declared_canon" not in types
    assert "L4_declared_canon_capped" in types
