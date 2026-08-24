from __future__ import annotations
from pathlib import Path
from .models import EmotionMarkerConfig

# repo_root/shared/assets/emotions.json (config.py -> emotion/ -> lifecycle/ -> root)
_DEFAULT_ASSET = Path(__file__).resolve().parents[2] / "shared" / "assets" / "emotions.json"


def load_emotion_config(asset_path: Path | None = None) -> EmotionMarkerConfig:
    import json

    if asset_path is None:
        asset_path = _DEFAULT_ASSET

    with open(asset_path, encoding="utf-8") as f:
        return EmotionMarkerConfig(**json.load(f))
