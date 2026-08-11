from __future__ import annotations

"""
Shared wiki utilities — config loading, type helpers, query builders.
Eliminates duplication across agent_wiki, file_wiki, user_wiki.
"""

import json
from pathlib import Path
from typing import Any


def load_config() -> dict:
    """Load config.yaml, return {} on failure."""
    try:
        import yaml

        config_path = Path(__file__).parent.parent / "config.yaml"
        with config_path.open() as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def get_enabled_types(layer: str, all_types: list[str]) -> list[str]:
    """Return wiki types enabled in config for the given layer."""
    cfg = load_config()
    layer_cfg = cfg.get("wiki", {}).get(layer, {})
    if not layer_cfg:
        return all_types
    return [t for t in all_types if layer_cfg.get(t, True)]


def get_external_dirs(layer: str) -> list[str]:
    """Return external directory paths from config for the given layer."""
    cfg = load_config()
    return cfg.get("wiki", {}).get(layer, {}).get("external_dirs", [])


ALLOWED_TABLES = {"user_wiki", "agent_wiki", "wiki_index"}


def parse_tags(raw_tags: Any) -> list[str]:
    """Parse tags from JSON string or list."""
    if isinstance(raw_tags, str):
        return json.loads(raw_tags) if raw_tags else []
    return raw_tags or []


def format_search_result(row: tuple, content_limit: int = 300) -> dict[str, Any]:
    """Format FTS search result row into dict."""
    return {
        "id": row[0],
        "title": row[1],
        "content": row[2][:content_limit],
        "type": row[3],
        "tags": parse_tags(row[4]),
        "importance": row[5],
        "score": abs(row[6]) if row[6] else 0,
    }
