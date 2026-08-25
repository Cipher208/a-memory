from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from shared.saga.schema import SagaState, SagaStatus, SagaStepState

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

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
        self._sync_step_states(state, steps)
        self.store.save(state)

        try:
            for i, step_def in enumerate(steps):
                state.current_step_index = i
                step_state = state.steps[i]
                if step_state.status == SagaStatus.COMPLETED:
                    # Resume: merge the persisted result, skip re-execution
                    state.context.update(step_state.result)
                    continue
                await self._execute_step(state, step_state, step_def)

            state.status = SagaStatus.COMPLETED
            self.store.save(state)
            return state.context
        except Exception:
            state.status = SagaStatus.FAILED
            self.store.save(state)
            await self.compensate(state, steps)
            raise

    def _sync_step_states(self, state: SagaState, steps: list[SagaStep]) -> None:
        """Append step states for definitions the persisted saga doesn't know yet."""
        while len(state.steps) < len(steps):
            step_def = steps[len(state.steps)]
            state.steps.append(SagaStepState(name=step_def.name, status=SagaStatus.PENDING))

    async def _execute_step(self, state: SagaState, step_state: SagaStepState, step_def: SagaStep) -> None:
        """Run one step with exponential-backoff retry; raises after exhaustion."""
        step_state.status = SagaStatus.RUNNING
        self.store.save(state)

        attempt, last_exc = 0, None
        while attempt <= step_def.retry_attempts:
            try:
                result: dict[str, Any] = await asyncio.wait_for(self._run_action(step_def, state), timeout=step_def.timeout_seconds)
                step_state.result = result or {}
                step_state.status = SagaStatus.COMPLETED
                state.context.update(step_state.result)
                self.store.save(state)
                return
            except Exception as e:
                last_exc = e
                attempt += 1
                if attempt > step_def.retry_attempts:
                    break
                await asyncio.sleep(step_def.retry_backoff * (2 ** (attempt - 1)))

        # Re-raise the original exception, preserving its type for callers
        step_state.status = SagaStatus.FAILED
        step_state.error = str(last_exc)
        self.store.save(state)
        assert last_exc is not None  # loop only ends via return or exception
        raise last_exc

    async def _run_action(self, step_def: SagaStep, state: SagaState) -> dict[str, Any]:
        res = step_def.action(state.context)
        return await res if asyncio.iscoroutine(res) else res

    async def compensate(self, state: SagaState, steps: list[SagaStep]) -> None:
        state.status = SagaStatus.COMPENSATING
        self.store.save(state)
        for i in range(state.current_step_index, -1, -1):
            if i >= len(state.steps):
                continue
            step_state, step_def = state.steps[i], steps[i]
            if step_state.status == SagaStatus.COMPLETED and step_def.compensation:
                await self._compensate_one(state, step_state, step_def)
        state.status = SagaStatus.COMPENSATED
        self.store.save(state)

    async def _compensate_one(self, state: SagaState, step_state: SagaStepState, step_def: SagaStep) -> None:
        try:
            await asyncio.wait_for(self._run_compensation(step_def, state), timeout=step_def.timeout_seconds)
        except Exception:
            logger.exception(f"Saga {state.saga_id}: compensation for {step_def.name} failed")

    async def _run_compensation(self, step_def: SagaStep, state: SagaState) -> None:
        if step_def.compensation:
            res = step_def.compensation(state.context)
            if asyncio.iscoroutine(res):
                await res
