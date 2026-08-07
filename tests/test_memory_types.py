"""Tests for shared/memory_types.py — 13 typed memory categories."""

import math

import pytest

from shared.memory_types import (
    _REGISTRY,
    MemoryKind,
    apply_decay,
    boost_for_query,
    can_archive,
    get_policy,
    kind_for_text,
    validate_kind,
)


def test_all_kinds_have_policy():
    for k in MemoryKind:
        assert k in _REGISTRY, f"missing policy for {k}"


def test_validate_kind_accepts_known_rejects_unknown():
    assert validate_kind("fact")
    assert validate_kind("instruction")
    assert not validate_kind("banana")
    assert not validate_kind("")


def test_default_kind_recovery():
    p_banana = get_policy("banana")
    p_fact = get_policy("fact")
    assert p_banana == p_fact


def test_apply_decay_fact_exponential():
    days = 30
    val = apply_decay(0.5, "fact", days)
    expected = max(0.01, 0.5 * math.exp(-0.01 * days))
    assert val == pytest.approx(expected, abs=1e-6)


def test_can_archive_fact():
    assert can_archive("fact", 0.1, days_since_update=200)
    assert not can_archive("fact", 0.9, days_since_update=200)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("я обещаю сделать к пятнице", MemoryKind.COMMITMENT),
        ("I commit to ship by Friday", MemoryKind.COMMITMENT),
        ("запрещено удалять базы данных", MemoryKind.RULE),
        ("do not push to main", MemoryKind.RULE),
        ("моя цель — выучить Rust", MemoryKind.GOAL),
        ("что-то нейтральное", MemoryKind.FACT),
    ],
)
def test_kind_for_text(text, expected):
    assert kind_for_text(text) == expected


def test_boost_for_query_prefers_matching_type():
    boost_pref = boost_for_query("что я предпочитаю?", "preference")
    boost_fact = boost_for_query("что я предпочитаю?", "fact")
    assert boost_pref > boost_fact


def test_boost_capped():
    boost = boost_for_query("обязательно никогда не забывай important rule", "instruction")
    assert boost <= 0.5
