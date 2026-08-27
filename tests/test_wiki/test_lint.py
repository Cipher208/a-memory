"""Tests for wiki.lint — 6 schema checks + auto_fix.

Mirrors tests/test_wiki/test_manager.py pattern: real filesystem,
real WikiManager, no mocks.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from wiki.lint import (
    Finding,
    LintReport,
    WIKI_LINT_TAG_BASELINE,
    auto_fix_type_dirs,
    lint_entry,
    lint_missing_index,
    lint_wiki_layer,
    wiki_lint_tag_vocabulary,
)
from wiki.manager import WikiManager
from wiki.parser import WikiParser


# ── Vocabulary ──────────────────────────────────────────────

def test_baseline_has_seven_tags():
    assert len(WIKI_LINT_TAG_BASELINE) == 7


def test_vocabulary_includes_baseline_and_enabled_types():
    vocab = wiki_lint_tag_vocabulary(["decision_log", "emotional_context"])
    assert "decision" in vocab
    assert "decision_log" in vocab  # from enabled_types
    assert "emotional_context" in vocab
    assert "unknown_xyz" not in vocab


# ── Per-entry checks ────────────────────────────────────────

def _entry(title: str = "T", wiki_type: str = "diary", content: str = "x", tags: list[str] | None = None) -> "WikiEntry":
    from wiki.models import WikiEntry
    return WikiEntry(
        entry_id=None,
        wiki_type=wiki_type,
        title=title,
        content=content,
        file_path="",
        tags=tags or [],
        importance=0.5,
        created_at=0.0,
        updated_at=0.0,
    )


def test_lint_clean_entry_no_findings():
    e = _entry(title="My Page", wiki_type="diary", content="hello world", tags=["draft"])
    findings = lint_entry(e, all_titles={"My Page", "Other"}, enabled_types=["diary"])
    assert findings == []


def test_lint_missing_title_when_falls_back_to_untitled():
    # When parser sees no title and no filename stem, it produces "Untitled"
    e = _entry(title="Untitled")
    findings = lint_entry(e, all_titles=set(), enabled_types=["diary"])
    assert any(f.code == "missing_title" for f in findings)


def test_lint_unknown_tag_against_vocabulary():
    e = _entry(tags=["decision", "zzz_made_up"])
    findings = lint_entry(e, all_titles=set(), enabled_types=["diary"])
    codes = [f.code for f in findings]
    assert "unknown_tag" in codes
    # "decision" is in baseline → no flag; "zzz_made_up" is unknown → flagged
    flagged_tags = [f.location for f in findings if f.code == "unknown_tag"]
    assert "tag:zzz_made_up" in flagged_tags
    assert "tag:decision" not in flagged_tags


def test_lint_unknown_tag_against_enabled_types():
    # Tag equals an enabled wiki_type name → known
    e = _entry(tags=["decision_log"])
    findings = lint_entry(e, all_titles=set(), enabled_types=["decision_log", "emotional_context"])
    assert not any(f.code == "unknown_tag" for f in findings)


def test_lint_broken_wikilink():
    e = _entry(content="See [[does_not_exist]] for context", title="T", wiki_type="diary")
    findings = lint_entry(e, all_titles={"T", "Other"}, enabled_types=["diary"])
    assert any(f.code == "broken_wikilink" and "does_not_exist" in f.message for f in findings)


def test_lint_broken_wikilink_skipped_when_no_titles():
    # If we don't have an index (all_titles=None), don't false-positive
    e = _entry(content="See [[anything]] for context", title="T", wiki_type="diary")
    findings = lint_entry(e, all_titles=None, enabled_types=["diary"])
    assert not any(f.code == "broken_wikilink" for f in findings)


def test_lint_page_too_long():
    e = _entry(content="x" * 50_001, title="T", wiki_type="diary")
    findings = lint_entry(e, all_titles=set(), enabled_types=["diary"])
    assert any(f.code == "page_too_long" for f in findings)


def test_lint_never_raises_on_garbage():
    # Even with completely weird inputs, lint_entry returns a list
    e = _entry(title="", wiki_type="", content="", tags=None)  # type: ignore[arg-type]
    findings = lint_entry(e, all_titles=None, enabled_types=None)
    assert isinstance(findings, list)


# ── missing_index ───────────────────────────────────────────

def test_lint_missing_index(tmp_path: Path):
    type_dir = tmp_path / "diary"
    type_dir.mkdir()
    f = lint_missing_index(type_dir)
    assert f is not None
    assert f.code == "missing_index"
    assert f.fixable is True


def test_lint_missing_index_skipped_when_present(tmp_path: Path):
    type_dir = tmp_path / "diary"
    type_dir.mkdir()
    (type_dir / "INDEX.md").write_text("# Index\n")
    assert lint_missing_index(type_dir) is None


# ── auto_fix_type_dirs ─────────────────────────────────────

def test_auto_fix_creates_index_stubs(tmp_path: Path):
    # 3 type dirs, none with INDEX.md
    for t in ["diary", "decision_log", "emotional_context"]:
        (tmp_path / t).mkdir()
    created = auto_fix_type_dirs(tmp_path, ["diary", "decision_log", "emotional_context"])
    assert len(created) == 3
    for t in ["diary", "decision_log", "emotional_context"]:
        assert (tmp_path / t / "INDEX.md").exists()


def test_auto_fix_idempotent(tmp_path: Path):
    (tmp_path / "diary").mkdir()
    created1 = auto_fix_type_dirs(tmp_path, ["diary"])
    created2 = auto_fix_type_dirs(tmp_path, ["diary"])
    assert len(created1) == 1
    assert created2 == []  # no second creation


# ── LintReport helpers ─────────────────────────────────────

def test_finding_severity_default():
    f = Finding(code="x", message="m", location="l")
    assert f.severity == "warning"
    assert f.fixable is False


def test_lint_report_by_code():
    r = LintReport(findings=[
        Finding(code="a", message="m1", location="l"),
        Finding(code="b", message="m2", location="l"),
        Finding(code="a", message="m3", location="l"),
    ])
    assert len(r.by_code("a")) == 2
    assert r.fixable_count == 0


# ── WikiManager integration ─────────────────────────────────

def test_manager_default_auto_fix_is_false():
    wm = WikiManager(layer="user")
    assert wm._auto_fix is False


def test_manager_auto_fix_true_accepted():
    wm = WikiManager(layer="user", auto_fix=True)
    assert wm._auto_fix is True
