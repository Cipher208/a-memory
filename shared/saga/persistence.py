from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Optional, Protocol, runtime_checkable
from pathlib import Path
import json

from shared.saga.schema import SagaState

@runtime_checkable
class ISagaStore(Protocol):
    """Abstract base class for Saga persistence."""

    def save(self, state: SagaState) -> None:
        """Save saga state to persistence."""
        ...

    def load(self, saga_id: str) -> Optional[SagaState]:
        """Load saga state by ID."""
        ...

    def delete(self, saga_id: str) -> None:
        """Delete saga state."""
        ...

    def list_all(self) -> List[SagaState]:
        """List all persisted sagas."""
        ...

class FileSagaStore:
    """File-based Saga storage with optional encryption."""

    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self, saga_id: str) -> Path:
        return self.storage_dir / f"{saga_id}.json"

    def save(self, state: SagaState) -> None:
        path = self._get_path(state.saga_id)
        data = state.model_dump()
        
        try:
            from shared.saga.impl.crypto import write_state_atomic
            write_state_atomic(path, data)
        except (ImportError, Exception):
            # Fallback to plain JSON if crypto fails or unavailable
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            tmp_path.replace(path)

    def load(self, saga_id: str) -> Optional[SagaState]:
        path = self._get_path(saga_id)
        if not path.exists():
            return None
        
        try:
            from shared.saga.impl.crypto import read_state_legacy_or_encrypted
            data = read_state_legacy_or_encrypted(path)
        except (ImportError, Exception):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        
        return SagaState(**data)

    def delete(self, saga_id: str) -> None:
        path = self._get_path(saga_id)
        if path.exists():
            path.unlink()

    def list_all(self) -> List[SagaState]:
        states = []
        for path in self.storage_dir.glob("*.json"):
            saga_id = path.stem
            state = self.load(saga_id)
            if state:
                states.append(state)
        return states
