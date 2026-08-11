from __future__ import annotations
from enum import Enum
import uuid
import time
from typing import Any
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
    status: SagaStatus = SagaStatus.PENDING
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class SagaState(BaseModel):
    saga_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str
    context: dict[str, Any] = Field(default_factory=dict)
    steps: list[SagaStepState] = Field(default_factory=list)
    current_step_index: int = 0
    status: SagaStatus = SagaStatus.PENDING
    started_at: float = Field(default_factory=time.time)
