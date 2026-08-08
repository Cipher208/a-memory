import json
from pathlib import Path
from lifecycle.emotion.models import EmotionMarkerConfig

def test_emotion_marker_config_loading():
    """Verify that emotions.json can be correctly loaded into EmotionMarkerConfig."""
    asset_path = Path(__file__).parent.parent.parent / "shared" / "assets" / "emotions.json"

    with open(asset_path, encoding="utf-8") as f:
        data = json.load(f)

    config = EmotionMarkerConfig(**data)

    # Assert markers
    assert "love" in config.markers
    assert "люблю" in config.markers["love"]
    assert "dear" in config.markers["love"]

    # Assert phrases
    love_phrases = [p for p in config.phrases if p.emotion == "love"]
    assert len(love_phrases) >= 4
    ru_love = [p for p in love_phrases if p.lang == "ru"]
    en_love = [p for p in love_phrases if p.lang == "en"]
    assert len(ru_love) > 0
    assert len(en_love) > 0

    # Assert emojis
    assert "joy" in config.emojis
    assert "😊" in config.emojis["joy"]

def test_phrase_pattern_validation():
    """Verify individual phrase pattern validation."""
    from lifecycle.emotion.models import PhrasePattern

    pattern = PhrasePattern(
        pattern=r"test (pattern)",
        emotion="joy",
        score=0.9,
        lang="en"
    )
    assert pattern.score == 0.9
    assert pattern.lang == "en"
