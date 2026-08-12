from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import EmotionEngine


class EmotionTrigger:
    """Facade for emotional and non-emotional message triggers.

    Re-implements logic from old lifecycle/emotion_trigger.py using EmotionEngine.
    """

    STATE_SHIFT_THRESHOLD = 0.15

    def __init__(self, engine: EmotionEngine):
        self.engine = engine

    def should_save(
        self, message: str, emotional_state: dict[str, float] | None = None, state_delta: dict[str, float] | None = None
    ) -> tuple[bool, str, float]:
        """Evaluate if a message should be saved based on emotional content and structural markers.

        Returns (should_save, trigger_name, score).
        """
        candidates: list[tuple[str, float]] = []

        # 1. Base emotions from engine
        engine_results = self.engine.detect(message)
        for res in engine_results:
            candidates.append((f"emotion_{res.trigger_type}", res.score))

        # 2. Non-emotional structural triggers
        if len(message) > 300:
            candidates.append(("long_message", 0.3))

        if message.count("?") >= 3:
            candidates.append(("complex_question", 0.4))

        if message.count("!") >= 2:
            candidates.append(("exclamation", 0.3))

        # 3. Contextual emotional state
        if emotional_state and (emotional_state.get("joy", 0) > 0.8 or emotional_state.get("interest", 0) > 0.8):
            candidates.append(("high_emotion", 0.6))

        # 4. State shifts
        if state_delta:
            for key, delta in state_delta.items():
                if abs(delta) > self.STATE_SHIFT_THRESHOLD:
                    candidates.append((f"state_shift_{key}", 0.4))

        if not candidates:
            return False, "", 0.0

        # Return the highest scoring trigger
        best_trigger, best_score = max(candidates, key=lambda x: x[1])
        return True, best_trigger, best_score
