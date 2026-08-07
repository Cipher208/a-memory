"""Tests for Importance v2 — multi-signal scorer."""

import pytest

from shared.importance import ImportanceScorer


@pytest.fixture
def scorer():
    return ImportanceScorer()


def test_noise_short_text_penalized(scorer):
    s = scorer.score("ok")
    assert s.noise_penalty > 0.9
    assert s.total() < 0.1


def test_noise_russian_ack_penalized(scorer):
    s = scorer.score("ага")
    assert s.total() < 0.15


def test_instruction_kind_has_high_base(scorer):
    s = scorer.score("инструкция", kind="instruction")
    assert s.base > 0.8
    assert s.emotional > 0.7


def test_commitment_kind_has_emotional_high(scorer):
    s = scorer.score("обещаю сделать", kind="commitment")
    assert s.emotional >= 0.8


def test_question_bonus(scorer):
    s1 = scorer.score("hello")
    s2 = scorer.score("what is this?")
    s3 = scorer.score("a? b? c?")
    assert s1.question == 0
    assert s2.question == 0.5
    assert s3.question == 1.0


def test_length_s_curve_capped(scorer):
    s_short = scorer.score("a" * 50)
    s_medium = scorer.score("a" * 400)
    s_long = scorer.score("a" * 5000)
    assert s_short.length < s_medium.length < s_long.length
    assert s_long.length == 1.0


@pytest.mark.parametrize(
    "text",
    [
        "Redis cluster на постгресе с JWT на /api/auth",
        "the redis postgres jwt oauth api is critical for production",
    ],
)
def test_technical_keywords(scorer, text):
    s = scorer.score(text)
    assert s.tech_keyword > 0.3


def test_novelty_seen_before_zero(scorer):
    s_first = scorer.score("hello world", seen_before=False)
    s_again = scorer.score("hello world", seen_before=True)
    assert s_first.novelty > 0
    assert s_again.novelty == 0


def test_retrieval_signal_log_scale(scorer):
    s0 = scorer.score("x", retrieval_count=0)
    s3 = scorer.score("x", retrieval_count=3)
    s100 = scorer.score("x", retrieval_count=100)
    assert 0 < s0.retrieval_signal < s3.retrieval_signal < s100.retrieval_signal
    assert s100.retrieval_signal <= 1.0


def test_total_in_unit_interval(scorer):
    for text in ["ok", "hello?", "redis jwt crash", ""]:
        s = scorer.score(text)
        assert 0.0 <= s.total() <= 1.0


def test_kind_auto_detection(scorer):
    s_fact = scorer.score("мой день рождения 15 июня")
    assert 0.4 <= s_fact.base <= 0.6

    s_commit = scorer.score("я обещаю сделать отчёт завтра")
    assert s_commit.base >= 0.8


def test_weights_override():
    custom = ImportanceScorer(weights={"tech_keyword": 2.5, "noise_penalty": 0.0})
    s = custom.score("ok")
    assert s.noise_penalty == 0.95
    default = ImportanceScorer().score("ok").total()
    assert s.total() > default


def test_custom_technical_keywords():
    custom = ImportanceScorer(technical_keywords_ru=("квантовый",))
    s = custom.score("квантовый двигатель")
    assert s.tech_keyword > 0.2
    default = ImportanceScorer().score("квантовый двигатель")
    assert default.tech_keyword < s.tech_keyword


def test_signal_breakdown_dict(scorer):
    s = scorer.score("redis cluster crash? need fix urgently")
    d = {
        "base": s.base,
        "length": s.length,
        "question": s.question,
        "tech_keyword": s.tech_keyword,
        "noise_penalty": s.noise_penalty,
    }
    assert all(0.0 <= v <= 1.0 for v in d.values())
