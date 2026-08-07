"""
Shared wiki utilities — config loading, type helpers, query builders.
Eliminates duplication across agent_wiki, file_wiki, user_wiki.
"""

import json
import time
from pathlib import Path
from typing import Any

from shared.connection import AsyncConnectionManager


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

async def find_by_source(cm: AsyncConnectionManager, table: str, user_id: str, source: str) -> int | None:
    """Find entry_id by user_id and source path in the given table."""
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Invalid table: {table}")
    conn = await cm.get("memory.db")
    sql = f"SELECT entry_id FROM {table} WHERE user_id=? AND source=?"  # noqa: S608
    cur = await conn.execute(sql, (user_id, source))
    row = await cur.fetchone()
    return row[0] if row else None


def parse_tags(raw_tags: Any) -> list[str]:
    """Parse tags from JSON string or list."""
    if isinstance(raw_tags, str):
        return json.loads(raw_tags) if raw_tags else []
    return raw_tags or []


def build_update_clause(fields: dict[str, Any]) -> tuple[list[str], list]:
    """Build SET clause and params for dynamic UPDATE.

    fields: dict of column_name -> value (None values skipped).
    Always includes updated_at. Returns (set_clauses, params).
    """
    updates = ["updated_at=?"]
    params: list = [time.time()]
    for col, val in fields.items():
        if col == "tags" and val is not None:
            updates.append(f"{col}=?")
            params.append(json.dumps(val))
        elif val is not None:
            updates.append(f"{col}=?")
            params.append(val)
    return updates, params


def build_count_query(table: str, user_id: str | None = None, wiki_type: str | None = None) -> tuple[str, list]:
    """Build COUNT query with optional WHERE clauses."""
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Invalid table: {table}")
    conditions, params = [], []
    if user_id:
        conditions.append("user_id=?")
        params.append(user_id)
    if wiki_type:
        conditions.append("wiki_type=?")
        params.append(wiki_type)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    sql = f"SELECT COUNT(*) FROM {table}{where}"  # noqa: S608
    return sql, params


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
