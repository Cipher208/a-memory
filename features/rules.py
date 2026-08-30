"""D1.9 memory rules engine — declarative YAML rules for the write gate.

Rules file: <data_dir>/rules.yaml:
    rules:
      - name: release-facts
        when_content_contains: ["release", "релиз"]
        importance_boost: 0.1
        tags: ["release"]
Applied in auto_save_text (the external write gate): matched rules add an
importance boost (sum, cap 0.3) and merge episode tags. mtime-cached;
missing file = empty ruleset (memory behavior unchanged). User-configurable
memory behavior without code.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_BOOST_CAP = 0.3

_cache: tuple[float, list[dict[str, Any]]] | None = None


def _rules_path() -> Path:
    from shared.connection import connection_manager

    return connection_manager.base_dir / "rules.yaml"


def load_rules(force: bool = False) -> list[dict[str, Any]]:
    """Load rules.yaml (mtime-cached). Malformed entries are skipped."""
    global _cache
    p = _rules_path()
    if not p.exists():
        _cache = (0.0, [])
        return []
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    if not force and _cache is not None and _cache[0] == mtime:
        return _cache[1]
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("rules.yaml parse failed: %s", exc)
        _cache = (mtime, [])
        return []
    if not isinstance(data, dict):
        # A valid-but-wrong-shape file (e.g. a bare list) degrades, never crashes.
        _cache = (mtime, [])
        return []
    rules = [
        r
        for r in (data.get("rules") or [])
        if isinstance(r, dict) and r.get("when_content_contains") and isinstance(r.get("when_content_contains"), list)
    ]
    _cache = (mtime, rules)
    return rules


def apply_rules(text: str) -> dict[str, Any]:
    """Match rules against text → {importance_boost, tags, matched}."""
    low = (text or "").lower()
    boost = 0.0
    tags: list[str] = []
    matched: list[str] = []
    for r in load_rules():
        needles = [str(t).lower() for t in (r.get("when_content_contains") or [])]
        if any(n in low for n in needles):
            matched.append(str(r.get("name") or f"rule_{len(matched)}"))
            boost = min(_BOOST_CAP, boost + float(r.get("importance_boost") or 0.0))
            for t in r.get("tags") or []:
                if t and t not in tags:
                    tags.append(str(t))
    return {"importance_boost": boost, "tags": tags, "matched": matched}
