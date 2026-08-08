import pytest
from lifecycle.emotion.trigger import EmotionTrigger
from lifecycle.emotion.config import load_emotion_config
from lifecycle.emotion.engine import EmotionEngine


@pytest.fixture
def emotion_trigger():
    config = load_emotion_config()
    engine = EmotionEngine(config)
    return EmotionTrigger(engine)


def test_long_message_trigger(emotion_trigger):
    message = "a" * 301
    should_save, trigger, score = emotion_trigger.should_save(message)
    assert should_save is True
    assert trigger == "long_message"
    assert score == 0.3


def test_complex_question_trigger(emotion_trigger):
    message = "What? How? Why?"
    should_save, trigger, score = emotion_trigger.should_save(message)
    assert should_save is True
    assert trigger == "complex_question"
    assert score == 0.4


def test_exclamation_trigger(emotion_trigger):
    message = "Wow!! This is great!!"
    # "great" might trigger joy (0.4 or phrase score)
    # If joy > 0.3, it might return joy.
    # Let's use a text that doesn't trigger emotions but has exclamations.
    message = "!! !!"
    should_save, trigger, score = emotion_trigger.should_save(message)
    assert should_save is True
    assert trigger == "exclamation"
    assert score == 0.3


def test_emotional_state_trigger(emotion_trigger):
    should_save, trigger, score = emotion_trigger.should_save("Hello", emotional_state={"joy": 0.9})
    assert should_save is True
    assert trigger == "high_emotion"
    assert score == 0.6


def test_state_shift_trigger(emotion_trigger):
    should_save, trigger, score = emotion_trigger.should_save("Hello", state_delta={"anger": 0.2})
    assert should_save is True
    assert trigger == "state_shift_anger"
    assert score == 0.4


def test_phrase_trigger(emotion_trigger):
    # "я тебя люблю" -> "love", 0.8
    should_save, trigger, score = emotion_trigger.should_save("я тебя люблю")
    assert should_save is True
    assert trigger == "emotion_love"
    assert score == 0.8


def test_marker_trigger(emotion_trigger):
    # "люблю" -> "love", 0.4 (default for marker)
    should_save, trigger, score = emotion_trigger.should_save("люблю")
    assert should_save is True
    assert trigger == "emotion_love"
    assert score == 0.4


def test_highest_score_wins(emotion_trigger):
    # message: "What? How? Why?" (complex_question, 0.4)
    # plus emotional state joy 0.9 (high_emotion, 0.6)
    should_save, trigger, score = emotion_trigger.should_save("What? How? Why?", emotional_state={"joy": 0.9})
    assert should_save is True
    assert trigger == "high_emotion"
    assert score == 0.6


def test_no_trigger(emotion_trigger):
    should_save, trigger, score = emotion_trigger.should_save("Just a normal message.")
    assert should_save is False
    assert trigger == ""
    assert score == 0.0
