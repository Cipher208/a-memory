"""Каждый, кто пишет в L0, обязан закрыть строку — а не только внешний хук.

`received` — единственный статус, который `l0_tiers` обещает НИКОГДА не тирировать
и не архивировать, и его никто не читает. Значит строка, оставленная в нём, не
«ждёт»: она копится без читателя и без срока. Водяной знак жил в `hooks/external.py`,
куда три других пути захвата не могли дотянуться, поэтому каждый из них оставлял
строки навсегда. Тесты ниже фиксируют, что закрывает каждый.

Проверено на живой базе до правки: три строки, пришедшие ПОСЛЕ рестарта
(20:28–20:40), всё ещё лежали `received` — `think` и агентский хук.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path, monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _rows(cm: Any) -> list[Any]:
    conn = await cm.get("memory.db")
    return list(await (await conn.execute("SELECT id, status, event, processed_at FROM l0_journal ORDER BY id")).fetchall())


@pytest.mark.asyncio
async def test_close_row_is_reachable_from_shared_not_only_the_hook(cm: Any) -> None:
    """Why the helper moved: three call sites could not import a hook-private one."""
    from shared.l0 import capture, close_row

    rid = await capture("think", "user", "u1", "строка без водяного знака")
    assert rid is not None
    await close_row(rid, "routed_direct")

    row = (await _rows(cm))[0]
    assert row["status"] == "routed_direct"
    assert row["processed_at"] is not None


@pytest.mark.asyncio
async def test_close_row_records_a_reason_when_there_is_one(cm: Any) -> None:
    """A refusal must be distinguishable from a row nobody ever looked at."""
    import json

    from shared.l0 import capture, close_row

    rid = await capture("new_message", "user", "u1", "отказная строка")
    assert rid is not None
    await close_row(rid, "gated_out", reason="importance_gate_bypass")

    conn = await cm.get("memory.db")
    decisions = json.loads((await (await conn.execute("SELECT decisions FROM l0_journal WHERE id=?", (rid,))).fetchone())[0])
    assert decisions[-1]["reason"] == "importance_gate_bypass"
    assert decisions[-1]["config_hash"]


@pytest.mark.asyncio
async def test_close_row_never_raises(cm: Any) -> None:
    """Best-effort like `capture`: a watermark failure must not break the write path."""
    from shared.l0 import close_row

    await close_row(999999, "routed_direct")  # строка не существует


@pytest.mark.asyncio
async def test_think_captures_and_closes(cm: Any) -> None:
    """`think` writes its own memory, so its row is finished the moment it lands.

    Calls the REAL `think`, not a copy of its body: a test that re-implements the
    code it claims to cover proves nothing. Before the fix the row stayed
    `received` with `skip_distill` in its decisions — a pair that says "handled
    elsewhere" and "still waiting" at once.
    """
    from unittest.mock import AsyncMock, MagicMock

    from mcp_server.tools.primitives.think import think

    ctx = MagicMock()
    app = MagicMock()
    app.rate_limiter.check = AsyncMock(return_value={"allowed": True})
    app.importance.score = MagicMock(return_value=MagicMock(score=0.9, signals=MagicMock(emotional=0.1)))
    app.hook_registry.fire = AsyncMock(return_value={})
    mem = app.mm.user_memory.return_value
    mem.remember = AsyncMock(return_value=1)
    mem.l3.save = AsyncMock()
    ctx.request_context.lifespan_context = app

    result = await think(text="Короткая важная мысль про архитектуру памяти", ctx=ctx)
    assert result["status"] == "ok", result

    think_rows = [r for r in await _rows(cm) if r["event"] == "think"]
    assert think_rows, "think обязан записать строку в L0"
    assert think_rows[0]["status"] == "routed_direct", think_rows[0]
    assert think_rows[0]["processed_at"] is not None


@pytest.mark.asyncio
async def test_remember_captures_and_closes(cm: Any) -> None:
    """`memory_remember` writes by key before journalling, so its row is finished too.

    Calls the real tool. Its row used to stay `received` for exactly the same reason
    `think`'s did: the watermark lived in a module this path does not import.
    """
    from unittest.mock import AsyncMock, MagicMock

    from mcp_server.tools.memory import memory_remember

    ctx = MagicMock()
    app = MagicMock()
    app.rate_limiter.check = AsyncMock(return_value={"allowed": True})
    app.importance.score = MagicMock(return_value=MagicMock(score=0.9, signals=MagicMock(emotional=0.1)))
    app.hook_registry.fire = AsyncMock(return_value={})
    app.mm.user_memory.return_value.remember = AsyncMock(return_value=1)
    ctx.request_context.lifespan_context = app

    result = await memory_remember(layer="user", user_id="u1", key="k1", value="значение, записанное по ключу", ctx=ctx)
    assert result["status"] == "ok", result

    rows = [r for r in await _rows(cm) if r["event"] == "remember"]
    assert rows, "remember обязан записать строку в L0"
    assert rows[0]["status"] == "routed_direct", rows[0]
    assert rows[0]["processed_at"] is not None


@pytest.mark.asyncio
async def test_the_agent_hook_closes_what_it_captures(cm: Any) -> None:
    """The third writer with no watermark: the agent-layer capture path.

    This one is the worst of the three, because it does not merely capture — it runs
    the whole distiller and then forgot to say so, so a row that genuinely produced
    memory still read as `received`. Found on a live base: two agent-layer rows from
    a running daemon, hours old, with EMPTY decisions.

    `mem is None` is exercised because that is the documented "only capture remains,
    the nightly will complete the write" branch, and it still owes the row a status.
    """
    from hooks.agent_hooks import AgentHooks

    hooks = AgentHooks(user_id="u1")
    await hooks._capture_route(None, "error_occurred", "ошибка при разборе конфига памяти", 0.8)

    rows = await _rows(cm)
    assert rows, "агентский хук обязан записать строку в L0"
    assert rows[0]["status"] == "routed_direct", rows[0]
    assert rows[0]["processed_at"] is not None


@pytest.mark.asyncio
async def test_the_expiry_is_the_backstop_for_a_path_that_forgets(cm: Any) -> None:
    """Even if a writer still forgets, no row accumulates forever any more.

    This is what makes the six call sites a maintenance problem rather than a
    correctness one: the nightly expiry closes what nobody closed.
    """
    from lifecycle.l0_tiers import close_overdue_received
    from shared.l0 import capture

    now = 1_800_000_000.0
    rid = await capture(
        "think", "user", "u1", "забытая строка другого пути", decisions=[{"gate": "think", "skip_distill": True}], ts_override=now - 30 * 86400
    )
    assert rid is not None
    assert (await _rows(cm))[0]["status"] == "received"

    assert (await close_overdue_received(now=now))["closed"] == 1
    assert (await _rows(cm))[0]["status"] == "gated_out"
