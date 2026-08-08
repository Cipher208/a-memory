import pytest
from shared.importance import ImportanceScorer, ImportanceConfig

@pytest.fixture
def scorer():
    return ImportanceScorer()

def test_scorer_all_weights_zero(scorer):
    """Verify that if all weights are zero, the score is 0.0."""
    zero_config = ImportanceConfig(
        weights={
            "base": 0.0,
            "tech_keyword": 0.0,
            "length": 0.0,
            "question": 0.0,
            "emotional": 0.0,
            "novelty": 0.0,
            "retrieval_signal": 0.0,
            "noise_penalty": 1.0
        }
    )
    scorer._config = zero_config

    text = "Some very important tech text about redis and docker!"
    result = scorer.score(text)
    assert result.score == 0.0

def test_scorer_empty_text(scorer):
    """Verify empty text handling."""
    # Current scorer logic: raw = sum_pos / max_possible
    # sum_pos for empty text is likely 0.0. 0.0 / 1.0 = 0.0.
    # noise_penalty for empty text is high (0.95-1.0)?
    # penalized = 0.0 * (1.0 - penalty) = 0.0.
    result = scorer.score("")
    # Just verify it doesn't crash and returns a valid result
    assert result.score >= 0.0

def test_scorer_signal_clamping(scorer):
    """Verify that signals are clamped to [0, 1] range."""
    # Length signal maxes at 800 chars = 1.0
    text = "a" * 2000
    result = scorer.score(text)
    assert result.signals.length == 1.0

    # Tech signal maxes at 4 keywords = 1.0
    text_tech = "redis docker postgres redis redis"
    result = scorer.score(text_tech)
    assert result.signals.tech_keyword == 1.0

def test_scorer_negative_weights_safety(scorer):
    """
    Verify that negative weights (if allowed by config) 
    don't result in negative final score due to clamping.
    """
    neg_config = ImportanceConfig(
        weights={
            "base": -1.0,
            "tech_keyword": 1.0,
            "length": 0.0,
            "question": 0.0,
            "emotional": 0.0,
            "novelty": 0.0,
            "retrieval_signal": 0.0,
            "noise_penalty": 1.0
        }
    )
    scorer._config = neg_config

    result = scorer.score("redis")
    assert result.score >= 0.0
