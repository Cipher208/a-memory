from typing import Any

from .base_signal import IImportanceSignal

# The _emotion_engine context key was never populated anywhere in production
# (grep 2026-09-13), so emotional scored 0.0 on every call site — the "intimate
# register under-gated" symptom traced here, not to weights. A lazy
# lexicon-backed default revives the signal for standalone scorers (think,
# distiller, replay); an injected engine still overrides it.
_DEFAULT_ENGINE: Any | None = None
_ENGINE_TRIED = False


def _default_engine() -> Any:
    global _DEFAULT_ENGINE, _ENGINE_TRIED
    if not _ENGINE_TRIED:
        _ENGINE_TRIED = True
        import contextlib

        with contextlib.suppress(Exception):
            from lifecycle.emotion import EmotionEngine, load_emotion_config

            _DEFAULT_ENGINE = EmotionEngine(load_emotion_config())
    return _DEFAULT_ENGINE


class EmotionSignal(IImportanceSignal):
    """Signal based on emotional intensity.

    Integration: uses _emotion_engine from context if available, else the
    package default lexicon engine (lazy, one instance per process).
    """

    def calculate(self, text: str, context: dict[str, Any]) -> float:
        engine = context.get("_emotion_engine")
        if engine is None and "emotion_weight" in context:
            # Explicit caller-provided weight wins over the default engine
            # (preserves the pre-2026-09-13 contract asserted by tests).
            val: Any = context.get("emotion_weight", 0.0)
            return float(max(0.0, min(1.0, float(val))))
        engine = engine or _default_engine()
        if not engine:
            val2: Any = context.get("emotion_weight", 0.0)
            return float(max(0.0, min(1.0, float(val2))))

        results = engine.detect(text)
        if not results:
            return 0.0

        # Use max score from detected emotions
        return float(max(float(res.score) for res in results))
