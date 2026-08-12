from typing import Any
from .base_signal import IImportanceSignal


class EmotionSignal(IImportanceSignal):
    """Signal based on emotional intensity.

    Integration: uses _emotion_engine from context if available.
    """

    def calculate(self, text: str, context: dict[str, Any]) -> float:
        engine = context.get("_emotion_engine")
        if not engine:
            # Fallback to provided emotion_weight if any
            val: Any = context.get("emotion_weight", 0.0)
            return float(max(0.0, min(1.0, float(val))))

        results = engine.detect(text)
        if not results:
            return 0.0

        # Use max score from detected emotions
        return float(max(float(res.score) for res in results))
