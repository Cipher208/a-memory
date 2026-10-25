"""Pure gap computation (C1.10 S5)."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from features.diff import compute_session_gaps
from shared.connection import connection_manager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


def _seed_log(tmp_path: Path, rows: list[tuple]) -> None:
    """rows: (event, source_msg_id, user_id, score, saved_l3, saved_l4, saved_graph, created_at[, text_preview])"""
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.executescript("""
        CREATE TABLE memory_dispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL DEFAULT 'default',
            score REAL,
            saved_l3 INTEGER NOT NULL DEFAULT 0,
            saved_l4 INTEGER NOT NULL DEFAULT 0,
            saved_graph INTEGER NOT NULL DEFAULT 0,
            text_preview TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
    """)
    for r in rows:
        if len(r) == 8:
            r = (*r, "")
        conn.execute(
            "INSERT INTO memory_dispatch_log (event, source_msg_id, user_id, score, saved_l3, saved_l4, saved_graph, created_at, text_preview) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


def _seed_rules(tmp_path: Path, rules: list[tuple[str, str]]) -> None:
    """rules: (trigger, predicate_json). Mirrors the migration's `watch_rules` shape."""
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watch_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            trigger TEXT NOT NULL,
            predicate TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT 'auto_save_text',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
    """)
    for trigger, predicate in rules:
        conn.execute(
            "INSERT INTO watch_rules (name, trigger, predicate, created_at) VALUES (?, ?, ?, ?)",
            (f"rule_{trigger}", trigger, predicate, time.time()),
        )
    conn.commit()
    conn.close()


class _FakeL3:
    def get(self, msg_id: int) -> dict:
        raise AssertionError("gap preview must come from the dispatch log, never L3 lookup")


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()


def test_above_both_thresholds_l4_only_is_gap_missing_l4(fresh_dir: Path) -> None:
    """score 0.9 (>= the L4 threshold 0.8), saved_l3=1, saved_l4=0 → missing=['l4']."""
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.9, 1, 0, 1, time.time(), "решили мигрировать базу")])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert len(gaps) == 1
    g = gaps[0]
    assert g["missing"] == ["l4"]
    assert "решили мигрировать" in g["text_preview"]


def test_band_between_the_two_thresholds_expects_no_l4(fresh_dir: Path) -> None:
    """THE REGRESSION: 0.6 is above the L3 bar and below the L4 bar — not a gap at all.

    The L4 branch used to reuse the L3 number (0.5), so every dispatch in the 0.5-0.8
    band was reported as a missing L4. That band is where L4 is not expected: on one
    live base it produced 313 of 439 `diff_gap` episodes (71%), all of them normal
    routing, and the owner read them as an open backlog.
    """
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.6, 1, 0, 1, time.time(), "обычный разговор")])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert gaps == [], "the 0.5-0.8 band is L3-only routing, not a missing L4"


def test_high_score_full_save_is_not_a_gap(fresh_dir: Path) -> None:
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.9, 1, 1, 1, time.time())])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert gaps == []


def test_empty_log_yields_no_gaps(fresh_dir: Path) -> None:
    _seed_log(fresh_dir, [])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert gaps == []


def test_no_saves_at_all_is_a_gap(fresh_dir: Path) -> None:
    """score 0.9 with saved_l3=0, saved_l4=0 → gap with missing=['l3','l4']."""
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.9, 0, 0, 0, time.time(), "текст сообщения")])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert len(gaps) == 1
    assert set(gaps[0]["missing"]) == {"l3", "l4"}


def test_l3_gap_alone_is_reported(fresh_dir: Path) -> None:
    """score 0.6 above the L3 bar with saved_l3=0 → 'l3' only, still a real gap."""
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.6, 0, 0, 0, time.time(), "текст сообщения")])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert len(gaps) == 1
    assert gaps[0]["missing"] == ["l3"]


def test_the_declared_predicate_governs_not_a_hardcoded_number(fresh_dir: Path) -> None:
    """Lower the declared L4 bar and the same 0.6 row becomes a gap again.

    This is what proves the thresholds are READ from `watch_rules` rather than the
    defaults merely happening to match the live rule.
    """
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.6, 1, 0, 1, time.time(), "обычный разговор")])
    _seed_rules(fresh_dir, [("new_message", '{"min_importance": 0.5, "l4_min_importance": 0.5}')])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert len(gaps) == 1, "with l4_min_importance=0.5 the 0.6 row must be reported"
    assert gaps[0]["missing"] == ["l4"]


def test_a_rule_without_the_l4_key_falls_back(fresh_dir: Path) -> None:
    """A rule naming only the L3 bar still gets 0.8 for L4 — the key is optional."""
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.6, 1, 0, 1, time.time(), "обычный разговор")])
    _seed_rules(fresh_dir, [("new_message", '{"min_importance": 0.5}')])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert gaps == [], "the L4 default (0.8) still excludes the 0.6 band"


def test_an_out_of_range_threshold_is_treated_as_absent(fresh_dir: Path) -> None:
    """A value outside [0, 1] cannot be allowed to invert the comparison.

    `_validate_predicate` refuses such a predicate at write time; a row that predates it
    (or was written around it) must fall back rather than make every row a gap.
    """
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.6, 1, 0, 1, time.time(), "обычный разговор")])
    _seed_rules(fresh_dir, [("new_message", '{"min_importance": 0.5, "l4_min_importance": 5}')])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert gaps == [], "an invalid L4 bar falls back to 0.8 instead of matching everything"


def test_a_malformed_predicate_falls_back(fresh_dir: Path) -> None:
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.9, 1, 0, 1, time.time(), "текст")])
    _seed_rules(fresh_dir, [("new_message", "not json at all")])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert len(gaps) == 1 and gaps[0]["missing"] == ["l4"], "defaults still apply"


def test_empty_preview_is_not_a_gap(fresh_dir: Path) -> None:
    """Preview-less rows are noise markers, not gaps.

    Prod evidence (2026-09-12): the broken retroactive L3 lookup made every
    gap preview empty — 13,205 empty `diff_gap:` episodes in one base.
    """
    _seed_log(
        fresh_dir,
        [
            ("new_message", 1, "u1", 0.9, 1, 0, 1, time.time(), ""),
            ("new_message", 2, "u1", 0.9, 1, 0, 1, time.time(), "   "),
        ],
    )
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert gaps == []


def test_other_events_ignored(fresh_dir: Path) -> None:
    _seed_log(fresh_dir, [("session_started", None, "u1", 0.9, 1, 1, 1, time.time())])
    gaps = compute_session_gaps(_FakeMem(), since=0, until=time.time() + 1, user_id="u1")
    assert gaps == []


def test_time_window_filters_old_log_rows(fresh_dir: Path) -> None:
    old = time.time() - 7200
    _seed_log(fresh_dir, [("new_message", 1, "u1", 0.9, 1, 0, 1, old)])
    gaps = compute_session_gaps(_FakeMem(), since=time.time() - 60, until=time.time() + 1, user_id="u1")
    assert gaps == []
