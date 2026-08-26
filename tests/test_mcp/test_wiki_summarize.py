"""Tests for wiki_summarize — 6-perspective digest over real WikiManager.

Mirrors the pattern in tests/test_mcp/test_primitives.py and
tests/test_wiki/test_manager.py: real stores, no mocks, in-process DB.
"""
from __future__ import annotations

import pytest

from mcp_server.tools.wiki_summarize import (
    PERSPECTIVE_TO_TYPE,
    _validate_perspective,
    wiki_summarize,
)


# ── Pure-function tests (no DB) ──────────────────────────────────────

def test_perspective_to_type_has_exactly_six_entries():
    assert len(PERSPECTIVE_TO_TYPE) == 6


@pytest.mark.parametrize(
    "perspective,expected_layer,expected_type",
    [
        ("practical",     "agent", "decision_log"),
        ("epistemic",     "agent", "learning_journal"),
        ("psychological", "agent", "emotional_context"),
        ("social",        "user",  "relationships"),
        ("temporal",      "user",  "retrospective"),
        ("metacognitive", "agent", "principle_log"),
    ],
)
def test_perspective_mapping_canonical(perspective, expected_layer, expected_type):
    layer, wiki_type = PERSPECTIVE_TO_TYPE[perspective]
    assert layer == expected_layer
    assert wiki_type == expected_type


def test_validate_perspective_known():
    assert _validate_perspective("practical") == "practical"


def test_validate_perspective_unknown_raises():
    with pytest.raises(ValueError, match="Unknown perspective"):
        _validate_perspective("narrative")


def test_each_perspective_maps_to_distinct_type():
    types = [t for _, t in PERSPECTIVE_TO_TYPE.values()]
    assert len(set(types)) == 6, "Many-to-one violated: duplicate wiki_type in mapping"
