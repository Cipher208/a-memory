"""E18: DREAM markers anchored to message start — document-fragment junk eliminated."""

from features.importance import detect_dream_marker


def test_leading_marker_detected():
    res = detect_dream_marker("DREAM: skill: tune postgres via EXPLAIN first")
    assert res == {"target": "skill", "content": "tune postgres via EXPLAIN first"}


def test_leading_whitespace_tolerated():
    res = detect_dream_marker("  \n DREAM: fact: vps has 64GB ram")
    assert res is not None and res["target"] == "fact"


def test_mid_text_marker_rejected():
    """The E18 root fix: a marker buried in document text is NOT a signal."""
    doc = (
        "Read the section carefully. The roadmap mentions `DREAM: skill: → promote` "
        "as a pipeline step, then continues for several paragraphs of unrelated content."
    )
    assert detect_dream_marker(doc) is None


def test_case_insensitive_still_works():
    res = detect_dream_marker("dream: memory: server migrated to vm1282008")
    assert res is not None and res["target"] == "memory"


def test_no_marker():
    assert detect_dream_marker("plain message with no protocol") is None
    assert detect_dream_marker("") is None
