from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IImportanceSignal(Protocol):
    """Protocol for importance signals."""

    def calculate(self, text: str, context: dict[str, Any]) -> float:
        """Calculate signal value in range [0, 1]."""
        ...
