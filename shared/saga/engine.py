from __future__ import annotations
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING
from collections.abc import Callable, Coroutine
import asyncio
import logging
import time

from shared.saga.schema import SagaState, SagaStatus, SagaStepState

if TYPE_CHECKING:
    from shared.saga.persistence import ISagaStore

logger = logging.getLogger(__name__)


@dataclass
class SagaStep:
    name: str
    action: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]] | dict[str, Any]]
    compensation: Callable[[dict[str, Any]], Coroutine[Any, Any, None] | None] | None = None
    retry_attempts: int = 0
    retry_backoff: float = 1.0
    timeout_seconds: float = 300.0


class SagaEngine:
    def __init__(self, store: ISagaStore):
        self.store = store

    async def execute(self, state: SagaState, steps: list[SagaStep]) -> dict[str, Any]:
        if state.status in (SagaStatus.COMPLETED, SagaStatus.COMPENSATED):
            return state.context
        state.status = SagaStatus.RUNNING
        state.started_at = state.started_at or time.time()
        self.store.save(state)
        while len(state.steps) < len(steps):
            step_def = steps[len(state.steps)]
            state.steps.append(SagaStepState(name=step_def.name, status=SagaStatus.PENDING))
        self.store.save(state)
        try:
            for i, step_def in enumerate(steps):
                state.current_step_index = i
                step_state = state.steps[i]
                if step_state.status == SagaStatus.COMPLETED:
                    state.context.update(step_state.result)
                    continue
                step_state.status = SagaStatus.RUNNING
                self.store.save(state)
                attempt, last_error = 0, None
                while attempt <= step_def.retry_attempts:
                    try:

                        async def _run(s=step_def):
                            res = s.action(state.context)
                            return await res if asyncio.iscoroutine(res) else res

                        result = await asyncio.wait_for(_run(), timeout=step_def.timeout_seconds)
                        step_state.result = result or {}
                        step_state.status = SagaStatus.COMPLETED
                        state.context.update(step_state.result)
                        self.store.save(state)
                        break
                    except Exception as e:
                        last_error = str(e)
                        attempt += 1
                        if attempt <= step_def.retry_attempts:
                            await asyncio.sleep(step_def.retry_backoff * (2 ** (attempt - 1)))
                        else:
                            step_state.status = SagaStatus.FAILED
                            step_state.error = last_error
                            self.store.save(state)
                            raise
            state.status = SagaStatus.COMPLETED
            self.store.save(state)
            return state.context
        except Exception:
            state.status = SagaStatus.FAILED
            self.store.save(state)
            await self.compensate(state, steps)
            raise

    async def compensate(self, state: SagaState, steps: list[SagaStep]):
        state.status = SagaStatus.COMPENSATING
        self.store.save(state)
        for i in range(state.current_step_index, -1, -1):
            if i >= len(state.steps):
                continue
            step_state, step_def = state.steps[i], steps[i]
            if step_state.status == SagaStatus.COMPLETED and step_def.compensation:
                try:

                    async def _run_c(s=step_def):
                        res = s.compensation(state.context)
                        if asyncio.iscoroutine(res):
                            await res

                    await asyncio.wait_for(_run_c(), timeout=step_def.timeout_seconds)
                except Exception as e:
                    logger.error(f"Saga {state.saga_id}: compensation for {step_def.name} failed: {e}")
        state.status = SagaStatus.COMPENSATED
        self.store.save(state)
