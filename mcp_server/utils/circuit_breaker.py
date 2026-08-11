from __future__ import annotations

"""
Circuit Breaker pattern for LLM/embedding calls.

States:
  - closed: normal operation, requests pass through
  - open: failures exceeded threshold, requests blocked
  - half-open: recovery probe, one request allowed through

Usage:
    breaker = CircuitBreaker(threshold=3, recovery_timeout=30)

    if not breaker.allow_request():
        return cached_result or fallback

    try:
        result = await llm_call()
        breaker.record_success()
        return result
    except Exception as e:
        breaker.record_failure()
        raise
"""

import logging
import time
from enum import Enum
from typing import TYPE_CHECKING, Literal, Any

if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker with configurable threshold and recovery timeout."""

    def __init__(
        self,
        threshold: int = 3,
        recovery_timeout: float = 30.0,
        name: str = "default",
        on_state_change: Callable[[str, CircuitState, CircuitState], Any] | None = None,
    ):
        self.threshold = threshold
        self.recovery_timeout = recovery_timeout
        self.name = name

        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._last_failure_at = 0.0

        self._on_state_change = on_state_change

        # Metrics
        self._total_requests = 0
        self._total_failures = 0
        self._total_rejections = 0
        self._state_changes = 0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and time.time() - self._opened_at > self.recovery_timeout:
            self._transition_to(CircuitState.HALF_OPEN)
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    def _transition_to(self, new_state: CircuitState) -> None:
        old_state = self._state
        self._state = new_state
        self._state_changes += 1
        logger.info("CircuitBreaker[%s]: %s -> %s", self.name, old_state.value, new_state.value)
        if self._on_state_change:
            self._on_state_change(self.name, old_state, new_state)

    def record_success(self) -> None:
        self._total_requests += 1
        self._failures = 0
        if self._state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.CLOSED)

    def record_failure(self) -> None:
        self._total_requests += 1
        self._total_failures += 1
        self._failures += 1
        self._last_failure_at = time.time()

        if self._state == CircuitState.HALF_OPEN or self._failures >= self.threshold:
            self._transition_to(CircuitState.OPEN)
            self._opened_at = time.time()

    def allow_request(self) -> bool:
        self._total_requests += 1
        current_state = self.state

        if current_state == CircuitState.CLOSED:
            return True
        if current_state == CircuitState.HALF_OPEN:
            return True
        self._total_rejections += 1
        return False

    def get_metrics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self._failures,
            "threshold": self.threshold,
            "recovery_timeout": self.recovery_timeout,
            "total_requests": self._total_requests,
            "total_failures": self._total_failures,
            "total_rejections": self._total_rejections,
            "state_changes": self._state_changes,
            "last_failure_at": self._last_failure_at,
        }

    def __enter__(self) -> bool:
        self._context_allowed = self.allow_request()
        return self._context_allowed

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> Literal[False]:
        if exc_type is None:
            self.record_success()
        else:
            self.record_failure()
        return False


class CircuitBreakerRegistry:
    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(
        self,
        name: str,
        threshold: int = 3,
        recovery_timeout: float = 30.0,
        on_state_change: Callable[[str, CircuitState, CircuitState], Any] | None = None,
    ) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                threshold=threshold,
                recovery_timeout=recovery_timeout,
                name=name,
                on_state_change=on_state_change,
            )
        return self._breakers[name]

    def get_all(self) -> dict[str, CircuitBreaker]:
        return dict(self._breakers)

    def get_all_metrics(self) -> dict[str, dict[str, Any]]:
        return {name: breaker.get_metrics() for name, breaker in self._breakers.items()}

    def reset_all(self) -> None:
        for breaker in self._breakers.values():
            breaker._failures = 0
            breaker._state = CircuitState.CLOSED
            breaker._opened_at = 0.0


breaker_registry = CircuitBreakerRegistry()
