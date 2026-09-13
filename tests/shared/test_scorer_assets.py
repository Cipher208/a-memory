"""S2: scorer must load committed live assets, never an absolute foreign path."""

import json
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "shared" / "assets"


def test_assets_exist_and_valid():
    for name in ("importance_config.json", "importance.json"):
        p = ASSETS / name
        assert p.is_file(), f"missing {name}"
        json.loads(p.read_text(encoding="utf-8"))


def test_scorer_source_has_no_hardcoded_clone_path():
    src = (Path(__file__).resolve().parents[2] / "shared" / "importance" / "scorer.py").read_text(encoding="utf-8")
    assert "Projects/repos" not in src


def test_score_uses_live_tech_keywords():
    from shared.importance import ImportanceScorer

    # "redis" is in the committed importance.json tech lists; a failed asset
    # load raises FileNotFoundError from _load_data rather than degrading.
    r = ImportanceScorer().score("redis connection pool exhausted during migration rollback")
    assert r.signals.tech_keyword > 0.0


def test_missing_asset_raises_not_silent_fallback(tmp_path):
    from shared.importance import ImportanceScorer

    s = ImportanceScorer(config_path=str(tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError):
        s.score("some text about redis")
