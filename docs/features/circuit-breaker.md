# Circuit Breaker

## Overview

Prevents cascading failures when LLM or embedding services are unavailable.

Restored from commit `538c61b` (purged as unwired dead code in `94a7c52`) and
wired as of Phase E / E2.

## States

- closed: Normal operation
- open: Failures exceeded threshold, requests blocked
- half-open: Recovery probe, one request allowed

## Configuration

- threshold: 3 failures before opening
- recovery_timeout: 30 seconds before half-open

## Usage

from shared.circuit_breaker import CircuitBreaker

breaker = CircuitBreaker(threshold=3, recovery_timeout=30)

if not breaker.allow_request():
    return cached_result

try:
    result = await llm_call()
    breaker.record_success()
except Exception:
    breaker.record_failure()

## Wiring (E2)

Gates `EmbeddingCache._compute_missing_embeddings` (`shared/embeddings.py`):
3 consecutive `model.encode` failures → open 30s → hash-fallback vectors keep
recall serving (cached under the `hash-fallback/<model>` tag). Module
singleton: `_embedding_breaker`; metrics via
`shared.circuit_breaker.breaker_registry.get_all_metrics()`
(surfaced by `memory_diagnose`).

## Testing

pytest tests/test_shared/test_circuit_breaker.py tests/test_shared/test_embedding_breaker.py -v
