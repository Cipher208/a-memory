from __future__ import annotations
from pathlib import Path
from .models import EmotionMarkerConfig


def load_emotion_config(asset_path: Path | None = None) -> EmotionMarkerConfig:
    import json

    if asset_path is None:
        asset_path = Path("/home/murat/Projects/repos/mcp-ariel-memory/shared/assets/emotions.json")

    with open(asset_path, encoding="utf-8") as f:
        return EmotionMarkerConfig(**json.load(f))
