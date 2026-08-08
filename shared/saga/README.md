# Saga System

Refactored and moved to `shared/saga/`. Now features a modular async engine with state persistence and compensation logic.

## Architecture

The Saga system follows the **Saga Pattern** to manage distributed transactions or multi-step operations with rollback capabilities.

- **SagaEngine**: The core orchestrator that executes steps, handles retries, and triggers compensation on failure.
- **SagaStep**: Defines an action (async coroutine) and an optional compensation action.
- **Persistence**: Saga state is persisted via `ISagaStore` (SQL-based) with encrypted context support.

## Usage

```python
from shared.saga.engine import SagaEngine, SagaStep
from shared.saga.schema import SagaState

# Define steps
steps = [
    SagaStep(name="reserve_resource", action=reserve_fn, compensation=release_fn, retry_attempts=3),
    SagaStep(name="charge_user", action=charge_fn, compensation=refund_fn),
]

# Execute saga
engine = SagaEngine(store=my_store)
state = SagaState(saga_id="unique_id", context={"user_id": 123})
result = await engine.execute(state, steps)
```

## Features

- **Idempotency**: Completed steps are skipped on restart.
- **Exponential Backoff**: Configurable retries for transient failures.
- **Compensation**: Automatic reverse-order execution of compensation actions on failure.
- **Timeouts**: Per-step execution timeouts.
