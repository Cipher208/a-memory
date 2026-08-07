"""Tests for shared/embeddings.py — remaining unit tests."""

import pytest

from shared.embeddings import similarity


def test_similarity_zero_vector():
    v1 = [1.0, 0.0]
    v2 = [0.0, 0.0]
    assert similarity(v1, v2) == 0.0


def test_similarity_opposite():
    v1 = [1.0, 0.0]
    v2 = [-1.0, 0.0]
    assert similarity(v1, v2) == pytest.approx(-1.0)
