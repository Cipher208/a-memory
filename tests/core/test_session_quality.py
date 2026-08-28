"""Tests for core.session_quality (4-component 0-80 scorer)."""

from __future__ import annotations

import json
import pytest

from core.session_quality import (
    _component_formulas,
    compute_session_quality,
    _count_window,
    parts_to_json,
    parts_from_json,
)
from core.session import SessionStore
from core.episodic import EpisodicMemory
from core.memory import CoreMemory
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME


# ── Pure formula tests ────────────────────────────────────────────────


def test_formulas_zero_signals():
    parts = _component_formulas(
        message_count=0,
        duration_min=0,
        n_l4=0,
        n_l3=0,
        n_topics=0,
        n_state_deltas=0,
    )
    assert parts == {
        "depth": 0.0,
        "decision": 0.0,
        "linked_entries": 0.0,
        "user_engagement": 0.0,
    }


def test_formulas_depth_caps():
    parts = _component_formulas(
        message_count=20,
        duration_min=20,
        n_l4=0,
        n_l3=0,
        n_topics=0,
        n_state_deltas=0,
    )
    assert parts["depth"] == 20.0


def test_formulas_depth_mid():
    parts = _component_formulas(
        message_count=5,
        duration_min=3,
        n_l4=0,
        n_l3=0,
        n_topics=0,
        n_state_deltas=0,
    )
    assert parts["depth"] == 8.0


def test_formulas_decision_caps():
    parts = _component_formulas(
        message_count=0,
        duration_min=0,
        n_l4=10,
        n_l3=0,
        n_topics=0,
        n_state_deltas=0,
    )
    assert parts["decision"] == 20.0


def test_formulas_linked_and_engagement():
    parts = _component_formulas(
        message_count=0,
        duration_min=0,
        n_l4=0,
        n_l3=8,
        n_topics=2,
        n_state_deltas=1,
    )
    assert parts["linked_entries"] == 16.0
    assert parts["user_engagement"] == 15.0


def test_formulas_total_is_sum():
    parts = _component_formulas(
        message_count=5,
        duration_min=3,
        n_l4=2,
        n_l3=4,
        n_topics=1,
        n_state_deltas=2,
    )
    total = sum(parts.values())
    assert total == parts["depth"] + parts["decision"] + parts["linked_entries"] + parts["user_engagement"]


# ── JSON helpers ──────────────────────────────────────────────────────


def test_parts_json_roundtrip():
    parts = {"depth": 5.0, "decision": 12.0, "linked_entries": 4.0, "user_engagement": 10.0}
    blob = parts_to_json(parts)
    assert parts_from_json(blob) == parts


def test_parts_json_none_and_invalid():
    assert parts_from_json(None) is None
    assert parts_from_json("") is None
    assert parts_from_json("not json") is None
    assert parts_from_json("[1, 2]") is None  # not a dict


# ── DB integration: _count_window + compute_session_quality ──────────


@pytest.mark.asyncio
async def test_count_window_zero(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    await EpisodicMemory(cm=cm)._init_db()
    store = SessionStore(cm=cm)
    await store._init_db()
    n = await _count_window(cm, "default", 100.0, 200.0, "episodes")
    assert n == 0


@pytest.mark.asyncio
async def test_compute_session_quality_no_signals(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    await CoreMemory(cm=cm)._init_db()
    await EpisodicMemory(cm=cm)._init_db()
    store = SessionStore(cm=cm)
    await store._init_db()
    total, parts = await compute_session_quality(cm, "default", started_at=0.0, ended_at=30.0, message_count=0, topics=[], state_deltas={})
    assert total == 0.0
    assert parts == {
        "depth": 0.0,
        "decision": 0.0,
        "linked_entries": 0.0,
        "user_engagement": 0.0,
    }


# ── SessionStore integration ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_session_writes_score_and_parts(tmp_path):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    await CoreMemory(cm=cm)._init_db()
    await EpisodicMemory(cm=cm)._init_db()
    store = SessionStore(cm=cm)
    await store._init_db()

    sess_id = await store.create_session("default")
    now = 1_700_000_000.0
    # Insert directly so we control created_at inside the session window.
    conn = await cm.get(DB_NAME)
    await conn.execute(
        "INSERT INTO core_memory (user_id, layer, key, value, importance, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("default", "user", "k", "v", 0.9, now + 10, now + 10),
    )
    await conn.execute(
        "INSERT INTO episodes (user_id, layer, summary, emotional_weight, created_at) VALUES (?, ?, ?, ?, ?)",
        ("default", "user", "summary", 0.5, now + 20),
    )
    await conn.execute(
        "UPDATE sessions SET started_at=?, message_count=? WHERE session_id=?",
        (now - 100, 5, sess_id),
    )
    await conn.commit()

    await store.close_session(sess_id, summary="did things", topics=["alpha", "beta"], state_deltas={"step": 1})

    rows = await (
        await conn.execute(
            "SELECT quality_score, quality_parts FROM sessions WHERE session_id=?",
            (sess_id,),
        )
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    score = row["quality_score"]
    parts_json = row["quality_parts"]
    assert score is not None
    assert score > 0
    assert score <= 80
    parts = json.loads(parts_json)
    assert set(parts.keys()) == {"depth", "decision", "linked_entries", "user_engagement"}


@pytest.mark.asyncio
async def test_close_session_non_fatal_on_scoring_failure(tmp_path, caplog):
    cm = AsyncConnectionManager(base_dir=tmp_path)
    store = SessionStore(cm=cm)
    await store._init_db()
    sess_id = await store.create_session("default")

    conn = await cm.get(DB_NAME)
    # Force the count query to fail by dropping the core_memory table mid-flight.
    # _count_window asserts `table in {"episodes", "core_memory"}` so we go through
    # the real path: create core_memory, drop it, then call close_session.
    from core.memory import CoreMemory

    await CoreMemory(cm=cm)._init_db()
    await conn.execute("DROP TABLE core_memory")
    await conn.commit()

    with caplog.at_level("WARNING", logger="core.session_quality"):
        await store.close_session(sess_id, summary="ok", topics=[], state_deltas={})

    row = await (
        await conn.execute(
            "SELECT quality_score, ended_at FROM sessions WHERE session_id=?",
            (sess_id,),
        )
    ).fetchone()
    assert row["ended_at"] is not None
    assert row["quality_score"] is None
