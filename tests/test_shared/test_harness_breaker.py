"""S18: circuit breaker for harness adapters (TencentDB v9: 5 fails → 60s)."""

from __future__ import annotations

import pytest

from shared.harness_breaker import HarnessUnavailableError, harness_call, reset_breaker


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold() -> None:
    reset_breaker("test-agent")
    calls = {"n": 0}

    async def fail() -> int:
        calls["n"] += 1
        raise RuntimeError("conn refused")

    for _ in range(5):
        with pytest.raises(RuntimeError):
            await harness_call("test-agent", fail)
    assert calls["n"] == 5, "до порога — каждый вызов проходит"
    with pytest.raises(HarnessUnavailableError):
        await harness_call("test-agent", fail)
    assert calls["n"] == 5, "открытый breaker не тратит вызов"


@pytest.mark.asyncio
async def test_success_records_and_resets() -> None:
    reset_breaker("test-agent2")

    async def ok() -> str:
        return "fine"

    assert await harness_call("test-agent2", ok) == "fine"


@pytest.mark.asyncio
async def test_breakers_isolated_per_agent() -> None:
    """Открытый breaker одного агента не блокирует другого."""
    reset_breaker("iso-a")
    reset_breaker("iso-b")

    async def fail() -> None:
        raise RuntimeError("down")

    for _ in range(5):
        with pytest.raises(RuntimeError):
            await harness_call("iso-a", fail)
    with pytest.raises(HarnessUnavailableError):
        await harness_call("iso-a", fail)

    async def ok() -> str:
        return "up"

    assert await harness_call("iso-b", ok) == "up", "чужой breaker не задет"
