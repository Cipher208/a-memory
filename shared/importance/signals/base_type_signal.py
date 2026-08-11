from typing import Any
from shared.memory_types import get_policy, kind_for_text, MemoryKind
from .base_signal import IImportanceSignal


class BaseSignal(IImportanceSignal):
    """
    Signal based on MemoryKind policy.
    """

    def calculate(self, text: str, context: dict[str, Any]) -> float:
        kind = context.get("kind")
        if kind is None:
            kind = kind_for_text(text)
        elif isinstance(kind, str):
            try:
                kind = MemoryKind(kind)
            except ValueError:
                kind = MemoryKind.FACT

        policy = get_policy(kind)
        return policy.default_importance
