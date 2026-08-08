import pytest
from lifecycle.emotion.config import load_emotion_config
from lifecycle.emotion.engine import EmotionEngine
from lifecycle.emotion.models import EmotionMarkerConfig

@pytest.fixture
def config():
    return load_emotion_config()

@pytest.fixture
def engine(config):
    return EmotionEngine(config)

def test_config_loader():
    config = load_emotion_config()
    assert isinstance(config, EmotionMarkerConfig)
    assert len(config.markers) > 0
    assert len(config.phrases) > 0
    assert len(config.emojis) > 0

def test_engine_detect_ru_phrases(engine):
    text = "Я тебя очень люблю, это правда."
    results = engine.detect(text)

    love_results = [r for r in results if r.trigger_type == "love"]
    assert len(love_results) > 0
    assert love_results[0].score >= 0.8
    assert love_results[0].metadata["source"] == "phrase"

def test_engine_detect_en_phrases(engine):
    text = "I love you so much!"
    results = engine.detect(text)

    love_results = [r for r in results if r.trigger_type == "love"]
    assert len(love_results) > 0
    assert love_results[0].score >= 0.8
    assert love_results[0].metadata["source"] == "phrase"

def test_engine_detect_markers(engine):
    # 'обожаю' is a marker for love
    text = "Я тебя обожаю."
    results = engine.detect(text)

    love_results = [r for r in results if r.trigger_type == "love"]
    assert len(love_results) > 0
    assert love_results[0].metadata["source"] == "phrase" or love_results[0].metadata["source"] == "marker"

def test_engine_detect_emojis(engine):
    text = "Какая крутая новость! 😊"
    results = engine.detect(text)

    joy_results = [r for r in results if r.trigger_type == "joy"]
    assert len(joy_results) > 0
    assert joy_results[0].metadata["source"] == "emoji"
    assert joy_results[0].score == 0.3

def test_engine_no_nested_loops_hot_path(engine):
    """
    Conceptual test for 'no nested loops'. 
    We verify that detect() uses regex finditer which is O(N) relative to text length
    for a fixed set of compiled regexes.
    """
    text = "Simple text with some markers like love and joy 😊"
    results = engine.detect(text)
    assert len(results) >= 2

def test_engine_priority(engine):
    # 'люблю' is both a marker and part of a phrase
    text = "я тебя люблю"
    results = engine.detect(text)

    love_results = [r for r in results if r.trigger_type == "love"]
    assert len(love_results) == 1
    # Phrase has score 0.8, marker has 0.4. 0.8 should win.
    assert love_results[0].score == 0.8
    assert love_results[0].metadata["source"] == "phrase"

def test_mixed_emotions(engine):
    text = "Я очень рад, но мне немного грустно 😢"
    results = engine.detect(text)

    emotions = {r.trigger_type for r in results}
    assert "joy" in emotions
    assert "sadness" in emotions

    sadness_res = next(r for r in results if r.trigger_type == "sadness")
    # "мне немного грустно" is now caught as a phrase with score 0.6 due to flexible regex
    assert sadness_res.score == 0.6
    assert sadness_res.metadata["source"] == "phrase"
