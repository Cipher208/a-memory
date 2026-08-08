from typing import Protocol, runtime_checkable

@runtime_checkable
class IImportanceSignal(Protocol):
    """Protocol for importance signals."""
    def calculate(self, text: str, context: dict) -> float:
        """Calculate signal value in range [0, 1]."""
        ...
