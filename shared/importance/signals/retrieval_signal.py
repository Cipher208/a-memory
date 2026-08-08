import math
from .base_signal import IImportanceSignal


class RetrievalSignal(IImportanceSignal):
    """
    Signal based on retrieval frequency.
    """

    def calculate(self, text: str, context: dict) -> float:
        retrieval_count = context.get("retrieval_count", 0)
        if retrieval_count > 0:
            return min(1.0, math.log1p(retrieval_count) / 5.0)
        return 0.2
