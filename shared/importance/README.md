# Importance Scorer

Refactored and moved to `shared/importance/`. Implements a modular plugin architecture for multi-signal importance scoring.

## Architecture

The system calculates importance by aggregating signals from multiple independent plugins.

- **ImportanceScorer**: Orchestrator that executes all registered signals and weights them according to configuration.
- **Signals**: Modular plugins implementing the `IImportanceSignal` interface.
- **Weight System**: Dynamic weights for each signal allow for online learning and fine-tuning.

## Signals

Current implementation includes 8 signals:
1. **Base**: Static base score per memory type.
2. **Length**: Boosts longer, more descriptive messages.
3. **Question**: Detects interrogative intent.
4. **TechKeyword**: Matches technical terminology (RU/EN).
5. **Emotion**: Integrated with `lifecycle.emotion` for emotional weight.
6. **Novelty**: Boosts rare or surprising information (ITS-inspired).
7. **Retrieval**: Signal based on how often information is accessed.
8. **Noise**: Penalties for redundant or low-value patterns.

## Usage

```python
from shared.importance.scorer import ImportanceScorer

scorer = ImportanceScorer()
result = scorer.score("Critical fix for the auth module.")
print(f"Score: {result.score}")
```
