import pytest
from shared.importance import ImportanceScorer, ImportanceConfig
from lifecycle.emotion.engine import EmotionEngine
from lifecycle.emotion.models import EmotionMarkerConfig, PhrasePattern


@pytest.fixture
def emotion_engine():
    config = EmotionMarkerConfig(
        markers={"joy": ["ура", "круто"]},
        phrases=[PhrasePattern(pattern="я тебя люблю", emotion="love", score=0.9, lang="ru")],
        emojis={"happy": ["😊", "🚀"]},
    )
    return EmotionEngine(config)


def test_scorer_full_cycle():
    # Use real assets paths (relative to project root in tests)
    scorer = ImportanceScorer(config_path="shared/assets/importance_config.json", data_path="shared/assets/importance.json")

    # Test text with tech keywords and length
    text = "Нам нужно развернуть postgres и redis в docker для нашего нового API."
    result = scorer.score(text)

    assert result.score > 0.0
    assert result.signals.tech_keyword > 0.0
    assert result.signals.length > 0.0
    assert result.signals.noise_penalty == 0.0


def test_scorer_noise_penalty():
    scorer = ImportanceScorer()

    text = "ок"
    result = scorer.score(text)

    assert result.signals.noise_penalty > 0.9
    # Final score should be very low due to penalty
    assert result.score < 0.1


def test_scorer_with_emotion_engine(emotion_engine):
    scorer = ImportanceScorer()

    # Neutral text
    text_neutral = "обычный текст"
    res_neutral = scorer.score(text_neutral)

    # Emotional text
    text_emotional = "ура, это круто!"
    res_emotional = scorer.score(text_emotional, context={"_emotion_engine": emotion_engine})

    assert res_emotional.signals.emotional > res_neutral.signals.emotional
    assert res_emotional.score > res_neutral.score


def test_dynamic_weight_update():
    # Base weights: base=1.0, tech_keyword=1.0
    text = "postgres"

    # 1. Default config
    scorer = ImportanceScorer()
    res1 = scorer.score(text)

    # 2. Custom config with low weights
    low_config = ImportanceConfig(
        weights={
            "base": 0.1,
            "tech_keyword": 0.1,
            "length": 0.1,
            "question": 0.1,
            "emotional": 0.1,
            "novelty": 0.1,
            "retrieval_signal": 0.1,
            "noise_penalty": 1.0,
        }
    )
    scorer_custom = ImportanceScorer(config=low_config)
    res2 = scorer_custom.score(text)

    assert res2.score < res1.score


def test_tech_context_bonus():
    scorer = ImportanceScorer()
    text = "база данных"  # Should trigger some tech keywords if in importance.json

    res_normal = scorer.score(text)
    res_tech = scorer.score(text, context={"is_technical_context": True})

    # If "база данных" is not in keywords, tech_keyword might be 0.0 -> 0.3
    assert res_tech.signals.tech_keyword >= res_normal.signals.tech_keyword
