# Circuit Breaker

## Overview

Prevents cascading failures when LLM or embedding services are unavailable.

## States

- closed: Normal operation
- open: Failures exceeded threshold, requests blocked
- half-open: Recovery probe, one request allowed

## Configuration

- threshold: 3 failures before opening
- recovery_timeout: 30 seconds before half-open

## Usage

from mcp_server.utils.circuit_breaker import CircuitBreaker

breaker = CircuitBreaker(threshold=3, recovery_timeout=30)

if not breaker.allow_request():
    return cached_result

try:
    result = await llm_call()
    breaker.record_success()
except Exception:
    breaker.record_failure()

## Testing

pytest tests/test_circuit_breaker.py -v
