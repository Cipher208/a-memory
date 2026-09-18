"""Merge ranking: per-source normalization + recency (E3 tuning 18.09)."""

import time

from rag.multi_source import merge_ranked


def _r(source, score, age_days=None, title="t", content="c"):
    r = {"title": title, "content": content, "score": score, "source": source}
    if age_days is not None:
        r["created_at"] = time.time() - age_days * 86400
    return r


def test_fresh_episode_beats_stale_essay():
    old = _r("fts5", 0.95, age_days=45, title="old essay")
    fresh = _r("episodic", 0.05, age_days=1, title="fresh checkpoint")
    ranked = merge_ranked([old, fresh])
    assert ranked[0]["title"] == "fresh checkpoint"


def test_same_age_keeps_raw_order():
    a = _r("fts5", 0.9, age_days=5, title="a")
    b = _r("fts5", 0.3, age_days=5, title="b")
    ranked = merge_ranked([a, b])
    assert [r["title"] for r in ranked] == ["a", "b"]


def test_no_timestamp_sources_unaffected():
    a = _r("fts5", 0.8, title="a")
    b = _r("fts5", 0.2, title="b")
    ranked = merge_ranked([a, b])
    assert [r["title"] for r in ranked] == ["a", "b"]
