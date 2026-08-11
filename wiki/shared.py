from __future__ import annotations

"""
Shared wiki utilities — config loading, type helpers, query builders.
Eliminates duplication across agent_wiki, file_wiki, user_wiki.
"""

import json
from pathlib import Path
from typing import Any


def load_config() -> dict[str, Any]:
    """Load config.yaml, return {} on failure."""
    try:
        import yaml

        config_path = Path(__file__).parent.parent / "config.yaml"
        with config_path.open() as f:
            res: Any = yaml.safe_load(f)
            return dict(res) if isinstance(res, dict) else {}
    except (OSError, Exception):
        return {}


def get_enabled_types(layer: str, all_types: list[str]) -> list[str]:
    """Return wiki types enabled in config for the given layer."""
    cfg = load_config()
    wiki_cfg: dict[str, Any] = cfg.get("wiki", {})
    layer_cfg: dict[str, Any] = wiki_cfg.get(layer, {})
    if not layer_cfg:
        return all_types
    return [t for t in all_types if bool(layer_cfg.get(t, True))]


def get_external_dirs(layer: str) -> list[str]:
    """Return external directory paths from config for the given layer."""
    cfg = load_config()
    wiki_cfg: dict[str, Any] = cfg.get("wiki", {})
    layer_cfg: dict[str, Any] = wiki_cfg.get(layer, {})
    res: Any = layer_cfg.get("external_dirs", [])
    return [str(x) for x in res] if isinstance(res, list) else []


ALLOWED_TABLES = {"user_wiki", "agent_wiki", "wiki_index"}


def parse_tags(raw_tags: Any) -> list[str]:
    """Parse tags from JSON string or list."""
    if isinstance(raw_tags, str):
        res: Any = json.loads(raw_tags) if raw_tags else []
        return [str(x) for x in res] if isinstance(res, list) else []
    return [str(x) for x in raw_tags] if isinstance(raw_tags, list) else []


def format_search_result(row: tuple[Any, ...], content_limit: int = 300) -> dict[str, Any]:
    """Format FTS search result row into dict."""
    return {
        "id": row[0],
        "title": row[1],
        "content": str(row[2])[:content_limit],
        "type": row[3],
        "tags": parse_tags(row[4]),
        "importance": row[5],
        "score": abs(float(row[6])) if row[6] else 0.0,
    }
