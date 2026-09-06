"""S18: circuit breaker for harness adapters (TencentDB v9: 5 fails → 60s).

Hermes/cow/MiMoCode adapters wrap their ariel HTTP calls in harness_call;
5 consecutive failures open the breaker for 60s — HarnessUnavailableError
instead of hammering a dead ariel instance. Adapters opt in per agent name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from shared.circuit_breaker import breaker_registry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

T = TypeVar("T")

HARNESS_THRESHOLD = 5
HARNESS_RECOVERY_S = 60.0


class HarnessUnavailableError(RuntimeError):
    """Breaker open — ariel instance считается недоступным."""


def _breaker(agent: str) -> Any:
    return breaker_registry.get(f"harness:{agent}", threshold=HARNESS_THRESHOLD, recovery_timeout=HARNESS_RECOVERY_S)


def reset_breaker(agent: str) -> None:
    """Test/isolation helper: drop the agent's breaker (closed state)."""
    breaker_registry._breakers.pop(f"harness:{agent}", None)


async def harness_call(agent: str, fn: Callable[[], Awaitable[T]]) -> T:
    """Run an adapter call through the per-agent harness breaker."""
    b = _breaker(agent)
    if not b.allow_request():
        raise HarnessUnavailableError(f"harness breaker open for {agent!r}")
    try:
        out = await fn()
    except Exception:
        b.record_failure()
        raise
    b.record_success()
    return out
