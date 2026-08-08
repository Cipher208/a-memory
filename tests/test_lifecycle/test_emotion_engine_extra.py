import pytest
from lifecycle.emotion.engine import EmotionEngine
from lifecycle.emotion.models import EmotionMarkerConfig, PhrasePattern

@pytest.fixture
def engine():
    config = EmotionMarkerConfig(
        markers={"joy": ["ура"]},
        phrases=[
            PhrasePattern(pattern="я тебя люблю", emotion="love", score=0.9, lang="ru"),
            PhrasePattern(pattern="я тебя (очень )?люблю", emotion="love", score=0.9, lang="ru")
        ],
        emojis={"joy": ["😊"]}
    )
    return EmotionEngine(config)

def test_engine_regex_injection_safety(engine):
    """Verify that malformed user-like patterns don't crash the engine."""
    tricky_text = "Я тебя люблю" + "!" * 1000
    results = engine.detect(tricky_text)
    assert len(results) > 0

def test_engine_empty_input(engine):
    assert engine.detect("") == []
    assert engine.detect("   ") == []

def test_engine_case_insensitivity(engine):
    results = engine.detect("Я ТЕБЯ ЛЮБЛЮ")
    assert len(results) > 0
    assert results[0].trigger_type == "love"

def test_engine_marker_overlapping_phrase(engine):
    """
    If a marker 'ура' is part of a phrase 'ура победа', 
    the engine should ideally handle it according to priority rules.
    """
    # Adding a longer phrase
    engine.config.phrases.append(
        PhrasePattern(pattern="ура победа", emotion="joy", score=1.0, lang="ru")
    )
    # Re-compile regexes
    engine._compile() # It is _compile in the actual file, not _compile_regexes

    results = engine.detect("ура победа")
    # Should only return one joy result with score 1.0 (phrase) not 0.4 (marker)
    joy_results = [r for r in results if r.trigger_type == "joy"]
    assert len(joy_results) == 1
    assert joy_results[0].score == 1.0
    assert joy_results[0].metadata["source"] == "phrase"
