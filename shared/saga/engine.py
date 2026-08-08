from dataclasses import dataclass
from typing import List, Callable, Optional, Any, Coroutine, Dict
import asyncio
import logging
import time

from shared.saga.schema import SagaState, SagaStatus, SagaStepState
from shared.saga.persistence import ISagaStore

logger = logging.getLogger(__name__)

@dataclass
class SagaStep:
    name: str
    action: Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]] | Dict[str, Any]]
    compensation: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None] | None]] = None
    retry_attempts: int = 0
    retry_backoff: float = 1.0
    timeout_seconds: float = 300.0

class SagaEngine:
    def __init__(self, store: ISagaStore):
        self.store = store

    async def execute(self, state: SagaState, steps: List[SagaStep]) -> Dict[str, Any]:
        """
        Execute a list of steps for a given saga state.
        Implements idempotency, retries, and state persistence.
        """
        if state.status in (SagaStatus.COMPLETED, SagaStatus.COMPENSATED):
            return state.context

        state.status = SagaStatus.RUNNING
        state.started_at = state.started_at or time.time()
        self.store.save(state)

        # Ensure state.steps matches steps length
        while len(state.steps) < len(steps):
            step_def = steps[len(state.steps)]
            state.steps.append(SagaStepState(name=step_def.name, status=SagaStatus.PENDING))
        self.store.save(state)

        try:
            for i, step_def in enumerate(steps):
                state.current_step_index = i
                step_state = state.steps[i]

                if step_state.status == SagaStatus.COMPLETED:
                    logger.info(f"Saga {state.saga_id}: skipping completed step {step_def.name}")
                    state.context.update(step_state.result)
                    continue

                step_state.status = SagaStatus.RUNNING
                self.store.save(state)

                attempt = 0
                last_error = None
                
                while attempt <= step_def.retry_attempts:
                    try:
                        logger.info(f"Saga {state.saga_id}: executing step {step_def.name} (attempt {attempt})")
                        
                        async def _run_action():
                            action_res = step_def.action(state.context)
                            if asyncio.iscoroutine(action_res):
                                return await action_res
                            return action_res

                        result = await asyncio.wait_for(_run_action(), timeout=step_def.timeout_seconds)
                        
                        step_state.result = result or {}
                        step_state.status = SagaStatus.COMPLETED
                        step_state.error = None
                        state.context.update(step_state.result)
                        self.store.save(state)
                        break
                    except Exception as e:
                        last_error = str(e)
                        attempt += 1
                        if attempt <= step_def.retry_attempts:
                            delay = step_def.retry_backoff * (2 ** (attempt - 1))
                            logger.warning(f"Saga {state.saga_id}: step {step_def.name} failed, retrying in {delay}s: {e}")
                            await asyncio.sleep(delay)
                        else:
                            logger.error(f"Saga {state.saga_id}: step {step_def.name} failed after {step_def.retry_attempts} retries")
                            step_state.status = SagaStatus.FAILED
                            step_state.error = last_error
                            self.store.save(state)
                            raise e

            state.status = SagaStatus.COMPLETED
            self.store.save(state)
            return state.context

        except Exception as e:
            state.status = SagaStatus.FAILED
            self.store.save(state)
            logger.info(f"Saga {state.saga_id} failed, triggering compensation")
            await self.compensate(state, steps)
            raise e

    async def compensate(self, state: SagaState, steps: List[SagaStep]):
        """
        Perform compensation (rollback) for all completed steps in reverse order.
        """
        state.status = SagaStatus.COMPENSATING
        self.store.save(state)

        # Iterate backwards from the failed step
        for i in range(state.current_step_index, -1, -1):
            if i >= len(state.steps):
                continue
                
            step_state = state.steps[i]
            step_def = steps[i]

            if step_state.status == SagaStatus.COMPLETED and step_def.compensation:
                try:
                    logger.info(f"Saga {state.saga_id}: compensating step {step_def.name}")
                    
                    async def _run_compensation():
                        comp_res = step_def.compensation(state.context)
                        if asyncio.iscoroutine(comp_res):
                            await comp_res
                    
                    await asyncio.wait_for(_run_compensation(), timeout=step_def.timeout_seconds)
                except Exception as e:
                    logger.error(f"Saga {state.saga_id}: compensation for step {step_def.name} failed: {e}")
                    # We continue compensation for other steps even if one fails

        state.status = SagaStatus.COMPENSATED
        self.store.save(state)
