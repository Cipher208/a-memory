import json
from pathlib import Path

from shared.importance.models import ImportanceConfig, ImportanceSignals, ScorerResult

# Repo-relative, not an absolute host path: CI checkouts live elsewhere
# (was /home/murat/Projects/repos/... → FileNotFoundError on GitHub Actions).
ASSET_PATH = Path(__file__).resolve().parents[2] / "shared" / "assets" / "importance_config.json"


def test_importance_config_loading():
    with open(ASSET_PATH, encoding="utf-8") as f:
        data = json.load(f)

    config = ImportanceConfig(**data)
    assert "base" in config.weights
    assert config.weights["base"] == 1.0
    assert "high" in config.thresholds
    assert config.thresholds["high"] == 0.8


def test_importance_signals_defaults():
    signals = ImportanceSignals()
    assert signals.base == 0.0
    assert signals.length == 0.0


def test_scorer_result_serialization():
    signals = ImportanceSignals(base=0.5, length=0.2)
    result = ScorerResult(score=0.35, signals=signals)

    data = result.model_dump()
    assert data["score"] == 0.35
    assert data["signals"]["base"] == 0.5
    assert data["signals"]["length"] == 0.2
