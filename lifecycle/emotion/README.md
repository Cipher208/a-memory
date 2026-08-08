# Emotion Trigger

Refactored and moved to `lifecycle/emotion/`. Provides high-performance emotion detection using optimized regex matching.

## Architecture

The system uses a compiled regex engine to match phrases, word markers, and emojis against text.

- **EmotionEngine**: Main detector that compiles patterns into efficient regexes.
- **Priority System**: Detection follows a priority chain: **Phrases > Markers > Emojis**.
- **Scoring**: Different trigger types provide different confidence scores (Phrases use explicit scores, Markers 0.4, Emojis 0.3).

## Optimization

The engine uses **named groups** and **non-capturing groups with optional gaps** to allow flexible matching:
- `"я тебя люблю"` matches `"я тебя люблю"`, `"я тебя очень люблю"`, `"я тебя сильно люблю"`, etc.
- All patterns are compiled into a few large regexes for maximum throughput.

## Usage

```python
from lifecycle.emotion.engine import EmotionEngine
from lifecycle.emotion.models import EmotionMarkerConfig

config = EmotionMarkerConfig(...)
engine = EmotionEngine(config)

results = engine.detect("Я очень рад тебя видеть! 😊")
# [EmotionResult(trigger_type='joy', score=0.4, ...)]
```
