import json
from pathlib import Path
from .models import EmotionMarkerConfig

def load_emotion_config(asset_path: Path = None) -> EmotionMarkerConfig:
    """
    Loads emotion configuration from JSON.
    Priority:
    1. ~/.mcp-ariel-memory/emotions.json
    2. Provided asset_path
    3. shared/assets/emotions.json (fallback)
    """
    home_config = Path.home() / ".mcp-ariel-memory" / "emotions.json"

    if home_config.exists():
        config_path = home_config
    elif asset_path and asset_path.exists():
        config_path = asset_path
    else:
        # Fallback to shared assets relative to this file
        config_path = Path(__file__).parent.parent.parent / "shared" / "assets" / "emotions.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Emotion config not found at {config_path}")

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    return EmotionMarkerConfig.model_validate(data)
