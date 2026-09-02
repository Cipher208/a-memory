"""E2: circuit breaker — threshold opens, timeout half-opens, probe closes."""

import pytest

from shared.circuit_breaker import CircuitBreaker, CircuitState


def test_opens_after_threshold():
    b = CircuitBreaker(threshold=3, recovery_timeout=30.0)
    assert b.allow_request()
    for _ in range(3):
        b.record_failure()
    assert b.state == CircuitState.OPEN
    assert not b.allow_request()


def test_half_open_after_timeout_then_close():
    b = CircuitBreaker(threshold=1, recovery_timeout=0.0)
    b.record_failure()
    assert b.state == CircuitState.HALF_OPEN  # timeout elapsed immediately
    assert b.allow_request()
    b.record_success()
    assert b.state == CircuitState.CLOSED


def test_half_open_failure_reopens():
    b = CircuitBreaker(threshold=1, recovery_timeout=10.0)
    b.record_failure()
    assert b.state == CircuitState.OPEN
    assert not b.allow_request()
    b._opened_at -= 20.0  # age past the recovery timeout
    assert b.state == CircuitState.HALF_OPEN
    assert b.allow_request()  # probe allowed
    b.record_failure()  # probe failed → reopen (fresh _opened_at keeps it OPEN)
    assert b.state == CircuitState.OPEN
    assert not b.allow_request()


def test_metrics_and_reset():
    b = CircuitBreaker(threshold=2, recovery_timeout=30.0, name="t")
    b.record_failure()
    b.record_failure()
    m = b.get_metrics()
    assert m["state"] == "open" and m["total_failures"] == 2 and m["name"] == "t"
    b.reset()
    assert b.state == CircuitState.CLOSED
    assert b.get_metrics()["failures"] == 0


def test_context_manager_records():
    b = CircuitBreaker(threshold=1, recovery_timeout=30.0)
    with b as allowed:
        assert allowed
    assert b.get_metrics()["total_requests"] == 1
    with pytest.raises(RuntimeError), b:
        raise RuntimeError("boom")
    assert b.get_metrics()["total_failures"] == 1


def test_registry_get_is_singleton_per_name():
    from shared.circuit_breaker import CircuitBreakerRegistry

    reg = CircuitBreakerRegistry()
    b1 = reg.get("x", threshold=5)
    b2 = reg.get("x", threshold=1)  # existing instance wins, kwargs ignored
    assert b1 is b2 and b1.threshold == 5
    assert reg.get_all_metrics()["x"]["threshold"] == 5
