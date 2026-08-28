"""Tests for the recall_usage component of the session quality score."""
from __future__ import annotations

import pytest

from core.session_quality import _component_formulas


def test_recall_usage_zero():
    parts = _component_formulas(
        message_count=0, duration_min=0,
        n_l4=0, n_l3=0, n_topics=0, n_state_deltas=0, n_recall=0,
    )
    assert parts["recall_usage"] == 0.0
    assert sum(parts.values()) == 0.0


def test_recall_usage_caps():
    parts = _component_formulas(
        message_count=0, duration_min=0,
        n_l4=0, n_l3=0, n_topics=0, n_state_deltas=0, n_recall=10,
    )
    assert parts["recall_usage"] == 20.0


def test_recall_usage_mid():
    parts = _component_formulas(
        message_count=0, duration_min=0,
        n_l4=0, n_l3=0, n_topics=0, n_state_deltas=0, n_recall=2,
    )
    assert parts["recall_usage"] == 8.0
