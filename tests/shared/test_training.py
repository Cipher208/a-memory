"""Tests for shared.importance.training (training_value classifier)."""
from __future__ import annotations

from shared.importance.training import classify_training_value


def test_high_decision_and_outcome():
    assert classify_training_value("I decided to use sqlite WAL and it worked great") == "high"


def test_medium_only_decision():
    assert classify_training_value("I decided to use sqlite WAL") == "medium"


def test_medium_only_outcome():
    assert classify_training_value("the WAL mode works great") == "medium"


def test_low_neither():
    assert classify_training_value("just a random thought") == "low"


def test_case_insensitive():
    assert classify_training_value("We DECIDED to migrate") == "medium"


def test_ru_markers():
    assert classify_training_value("я решил использовать WAL и это получилось") == "high"
    assert classify_training_value("я выбрал подход") == "medium"


def test_word_boundary_undecided():
    assert classify_training_value("still undecided about the schema") == "low"


def test_empty_whitespace_low():
    assert classify_training_value("") == "low"
    assert classify_training_value("   ") == "low"
