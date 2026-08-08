import re
from unittest.mock import MagicMock
from shared.importance.signals.tech_signal import TechKeywordSignal
from shared.importance.signals.length_signal import LengthSignal
from shared.importance.signals.noise_signal import NoiseSignal
from shared.importance.signals.emotion_signal import EmotionSignal

def test_tech_signal():
    signal = TechKeywordSignal()
    tech_re = re.compile("redis|docker|postgres")

    # 0 hits
    assert signal.calculate("hello world", {"tech_re": tech_re}) == 0.0

    # 1 hit
    assert signal.calculate("using redis here", {"tech_re": tech_re}) == 0.25

    # 4 hits (maxed)
    assert signal.calculate("redis redis docker postgres", {"tech_re": tech_re}) == 1.0

    # Technical context bonus
    assert signal.calculate("redis", {"tech_re": tech_re, "is_technical_context": True}) == 0.55

def test_length_signal():
    signal = LengthSignal()
    assert signal.calculate("", {}) == 0.0
    assert signal.calculate("a" * 400, {}) == 0.5
    assert signal.calculate("a" * 800, {}) == 1.0
    assert signal.calculate("a" * 1000, {}) == 1.0

def test_noise_signal():
    signal = NoiseSignal()
    noise_re = re.compile(r"^ok\.?|^thanks?\.?", re.IGNORECASE)

    assert signal.calculate("ok", {"noise_re": noise_re}) == 0.95
    assert signal.calculate("Thanks.", {"noise_re": noise_re}) == 0.95
    assert signal.calculate("important info", {"noise_re": noise_re}) == 0.0

def test_emotion_signal_with_engine():
    signal = EmotionSignal()
    mock_engine = MagicMock()

    mock_res = MagicMock()
    mock_res.score = 0.7
    mock_engine.detect.return_value = [mock_res]

    assert signal.calculate("I love this!", {"_emotion_engine": mock_engine}) == 0.7
    mock_engine.detect.assert_called_once_with("I love this!")

def test_emotion_signal_fallback():
    signal = EmotionSignal()
    assert signal.calculate("text", {"emotion_weight": 0.5}) == 0.5
    assert signal.calculate("text", {}) == 0.0
