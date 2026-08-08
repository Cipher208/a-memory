from enum import Enum
from pydantic import BaseModel, Field

class SagaStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    STUCK = "stuck"

class SagaStepState(BaseModel):
    name: str
    status: SagaStatus
    result: dict = Field(default_factory=dict)
    error: str | None = None

class SagaState(BaseModel):
    saga_id: str
    name: str
    context: dict = Field(default_factory=dict)
    steps: list[SagaStepState] = Field(default_factory=list)
    current_step_index: int = 0
    status: SagaStatus = SagaStatus.PENDING
    started_at: float = Field(default_factory=float)
